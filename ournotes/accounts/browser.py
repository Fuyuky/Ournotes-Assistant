"""Public, read-only account browser. Never imports runtime_store or credentials."""

from __future__ import annotations

import asyncio
import contextlib
import math
from contextlib import asynccontextmanager
from typing import Literal

from fastapi import FastAPI, HTTPException, Query
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse

from ournotes.accounts.index import LOG, AccountIndex
from ournotes.accounts.pricing import price_sort_key
from ournotes.accounts.quality import BANDS, LOGO_FILES
from ournotes.paths import DATA_ROOT, ROOT

WEB = ROOT / "web/account-browser"


def create_app(
    accounts_dir=DATA_ROOT / "results/archive/accounts",
    catalog_dir=DATA_ROOT / "data/catalog/site",
    refresh_seconds=5,
    url_prefix="",
    account_metadata=None,
):
    index = AccountIndex(accounts_dir, catalog_dir)

    @asynccontextmanager
    async def lifespan(app):
        await asyncio.to_thread(index.refresh)

        async def watch():
            while True:
                await asyncio.sleep(refresh_seconds)
                await asyncio.to_thread(index.refresh)

        task = asyncio.create_task(watch())
        try:
            yield
        finally:
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task

    app = FastAPI(lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)
    app.state.index = index

    @app.middleware("http")
    async def headers(request, call_next):
        response = await call_next(request)
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Referrer-Policy"] = "no-referrer"
        response.headers["Content-Security-Policy"] = (
            "default-src 'self'; img-src 'self'; style-src 'self'; script-src 'self'; "
            "connect-src 'self'; object-src 'none'; base-uri 'none'; frame-ancestors 'none'"
        )
        response.headers["Cache-Control"] = "no-store" if request.url.path.startswith("/api/") else "no-cache"
        return response

    @app.exception_handler(RequestValidationError)
    async def invalid_request(request, error):
        return JSONResponse({"detail": "查询参数无效。"}, status_code=422)

    @app.exception_handler(Exception)
    async def server_error(request, error):
        LOG.error("Account browser request failed: %s", type(error).__name__)
        return JSONResponse({"detail": "暂时无法读取数据，请稍后重试。"}, status_code=500)

    @app.get("/")
    def home():
        return HTMLResponse(
            (WEB / "index.html").read_text("utf-8").replace('"/static/', f'"{url_prefix}/static/')
        )

    @app.get("/static/{name}")
    def static(name: str):
        if name not in ("app.js", "style.css", "data-source.js"):
            raise HTTPException(404, "资源不存在。")
        return FileResponse(WEB / name)

    @app.get("/art/{kind}/{card_id}")
    def art(kind: Literal["member", "support"], card_id: int):
        image = index.images.get(f"{kind}:{card_id}")
        if (
            image is None
            or not image.resolve().is_relative_to(index.catalog_dir / "images")
            or not image.is_file()
        ):
            raise HTTPException(404, "资源不存在。")
        return FileResponse(image)

    @app.get("/logos/{band}")
    def logo(band: str):
        if band not in LOGO_FILES:
            raise HTTPException(404, "资源不存在。")
        return FileResponse(WEB / "logos" / LOGO_FILES[band])

    @app.get("/api/cards")
    def cards():
        return {
            "cards": [{**card, "image": url_prefix + card["image"]} for card in index.cards.values()],
            "bands": {
                key: {"name": value["name"], **({"logo": f"{url_prefix}/logos/{key}"} if key in LOGO_FILES else {})}
                for key, value in BANDS.items()
            },
        }

    @app.get("/api/accounts")
    def accounts(
        cards: str = Query("", max_length=1024),
        order: Literal["desc", "asc", "price_desc", "price_asc", "selected_desc", "mygo_desc", "mujica_desc", "dream_desc", "millsage_desc", "dumb_desc"] = "price_desc",
        page: int = Query(1, ge=1),
        page_size: int = Query(10),
        index_filter: str = Query("", alias="index", max_length=16),
        include_used: bool = False,
        include_pending: bool = False,
        allow_missing: bool = False,
        pull_count: Literal['', '42', '43', '62'] = '',
    ):
        if page_size not in (10, 15, 20):
            raise HTTPException(422, "查询参数无效。")
        selected = set(filter(None, cards.split(",")))
        if not selected.issubset(index.cards):
            raise HTTPException(422, "筛选卡牌无效。")
        if order.startswith("selected_") and not selected:
            raise HTTPException(422, "请先选择至少一张卡牌。")
        snapshot = index.snapshot
        if index_filter and (
            not index_filter.isascii() or not index_filter.isdecimal() or int(index_filter) > 2**53 - 1
        ):
            raise HTTPException(422, "请输入有效的账号 index。")
        matched = [
            a
            for a in snapshot.accounts
            if (index_filter or include_used or not a["used"])
            and (index_filter or a["used"] or include_pending or a["price"]["status"] != "pending")
            and (not index_filter or a["index"] == int(index_filter))
            and (not pull_count or a.get('pull_count') == int(pull_count))
            and (not selected or (bool(selected.intersection(c["key"] for c in a["cards"]))
                 if allow_missing else selected.issubset(c["key"] for c in a["cards"])))
        ]
        if order.startswith("selected_"):
            matched = [{**a, "selected_count": sum(c["count"] for c in a["cards"] if c["key"] in selected)} for a in matched]
            matched.sort(key=lambda a: (
                -a["selected_count"],
                -len({c["key"] for c in a["cards"] if c["key"] in selected and c["count"] > 0}),
                *price_sort_key(a),
            ))
        elif order.startswith("price_"):
            matched.sort(key=lambda a: price_sort_key(a, ascending=order.endswith("asc")))
        elif order in ("mygo_desc", "mujica_desc", "dream_desc", "millsage_desc", "dumb_desc"):
            metric = f"{order.split('_')[0]}_characters"
            matched.sort(key=lambda a: (-a[metric], *price_sort_key(a)))
        else:
            matched.sort(key=lambda a: ((-1 if order == "desc" else 1) * a["ssr_total"], a["index"]))
        pages = max(1, math.ceil(len(matched) / page_size))
        page = min(page, pages)
        rows = matched[(page - 1) * page_size : page * page_size]
        if account_metadata is not None:
            rows = account_metadata(rows, snapshot)
        return {
            "accounts": rows,
            "total": len(matched),
            "page": page,
            "pages": pages,
            "page_size": page_size,
            "meta": {
                "valid_accounts": sum(not a["used"] for a in snapshot.accounts),
                "used_accounts": sum(a["used"] for a in snapshot.accounts),
                "total_valid_accounts": len(snapshot.accounts),
                "excluded_files": snapshot.excluded,
                "files": snapshot.files,
                "loaded_at": snapshot.loaded_at,
                "available": snapshot.available,
                "revision": snapshot.revision,
            },
        }

    return app
