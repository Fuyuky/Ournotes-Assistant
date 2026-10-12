"""Authenticated versioned coordination HTTP API, with a separate administrator credential."""
from __future__ import annotations

import hashlib
import hmac
import threading
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import Depends, FastAPI, Header, Request
from fastapi import Path as PathParameter
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, JSONResponse
from pydantic import Field

from ournotes.cloud.mail import GmailAuthorizationRequired, Tickets
from ournotes.cloud.models import (
    Allocation,
    HighWater,
    Historical,
    Model,
    Publish,
    ResultSubset,
    TicketRequest,
    TicketRetry,
    Upload,
    Usage,
    Used,
)
from ournotes.cloud.store import Conflict, Forbidden, Unavailable

PREFIX = "/api/cloud/v1"


class RebindRequest(Model):
    index: int = Field(ge=0, le=2**53 - 1)
    request_id: str = Field(pattern=r"^[a-f0-9]{32}$")
    purpose: str = Field(pattern=r"^(login|identity)$")


class NodeRequest(Model):
    node_id: str = Field(min_length=1, max_length=128, pattern=r"^[A-Za-z0-9_-]+$")


def create_app(store, admin_token, *, backend=None, rule=None, publisher=None, catalog=None, rebind_nodes=(),
               portal_credential=None, activity_catalog=None, manual_confirmer=None):
    if len(admin_token) < 32:
        raise ValueError("Administrator token must contain at least 32 characters")
    admin_hash = hashlib.sha256(admin_token.encode()).digest()
    tickets = Tickets(store, backend, rule)
    from ournotes.cloud.activities import Activities
    from ournotes.cloud.events import Events
    from ournotes.cloud.manual_mail import ManualMail
    from ournotes.cloud.presets import Presets
    events = Events(store)
    presets = Presets(store, catalog=activity_catalog or catalog)
    activities = Activities(store, activity_catalog or catalog)
    manual_mail = ManualMail(store, backend, rule, manual_confirmer)
    from ournotes.cloud.rebind import RebindTickets
    rebind = RebindTickets(store, backend, rule, rebind_nodes)
    validator = None
    if catalog is not None:
        from ournotes.accounts.index import AccountIndex
        validator = AccountIndex(Path(catalog) / "unused-cloud-source", catalog)
    store.result_validator = validator

    stop = threading.Event()

    def maintenance():
        while not stop.wait(15):
            tickets.cleanup()
            rebind.cleanup()
            if publisher:
                try:
                    publisher.publish_pending()
                except (OSError, ValueError, Conflict, Unavailable):
                    pass  # Keep the committed marker and retry from durable cloud state.

    @asynccontextmanager
    async def lifespan(app):
        tickets.cleanup()
        worker = threading.Thread(target=maintenance, daemon=True, name="cloud-ticket-expiry")
        worker.start()
        try:
            yield
        finally:
            stop.set()
            worker.join(timeout=2)

    app = FastAPI(title="OurNotes private coordination", version="1", lifespan=lifespan,
                  docs_url=None, redoc_url=None, openapi_url=None)
    app.state.store, app.state.tickets, app.state.publisher = store, tickets, publisher

    @app.exception_handler(RequestValidationError)
    async def validation_error(request, error):
        # FastAPI's default errors echo submitted data, including credentials/codes.
        return JSONResponse({"error": "invalid_request"}, status_code=422)

    @app.exception_handler(Conflict)
    async def conflict(request, error):
        return JSONResponse({"error": "conflict"}, status_code=409)

    @app.exception_handler(Forbidden)
    async def forbidden(request, error):
        return JSONResponse({"error": "forbidden"}, status_code=403)

    @app.exception_handler(Unavailable)
    async def unavailable(request, error):
        return JSONResponse({"error": "service_unavailable"}, status_code=503)

    @app.exception_handler(GmailAuthorizationRequired)
    async def gmail_authorization_required(request, error):
        return JSONResponse({'error': 'gmail_authorization_required'}, status_code=424)

    @app.exception_handler(KeyError)
    async def missing(request, error):
        return JSONResponse({"error": "not_found"}, status_code=404)

    @app.exception_handler(ValueError)
    async def invalid(request, error):
        return JSONResponse({"error": "invalid_request"}, status_code=422)

    def bearer(authorization: str = Header(default="")):
        if not authorization.startswith("Bearer ") or len(authorization) > 512:
            raise Forbidden("Credential required")
        return authorization[7:]

    bearer_dependency = Depends(bearer)

    def node(token=bearer_dependency):
        return store.authenticate(token)

    def admin(token=bearer_dependency):
        if not hmac.compare_digest(hashlib.sha256(token.encode()).digest(), admin_hash):
            raise Forbidden("Administrator credential required")
        return "admin"

    node_dependency, admin_dependency = Depends(node), Depends(admin)
    from ournotes.cloud.services import mount as mount_services
    mount_services(app, store, node_dependency, validator=validator, presets=presets, activities=activities,
                   events=events, manual_mail=manual_mail)
    app.state.presets, app.state.activities, app.state.events = presets, activities, events
    app.state.manual_mail = manual_mail

    def auto_publish(value):
        if publisher:
            try:
                value['publication'] = publisher.publish_pending()
            except (OSError, ValueError, Conflict, Unavailable):
                value['publication'] = {'pending': True}
        return value

    def validate_final(result):
        if validator:
            value = result.model_dump()
            validator.account({"schema_version": 1, "status": "complete", "alias_index": value["index"],
                               "used": value["used"], "pull_count": value["pull_count"],
                               "cards": value["cards"], "totals": value["totals"]})

    @app.middleware("http")
    async def privacy_headers(request: Request, call_next):
        # Reject oversize bodies before parsing. Nginx provides a second hard limit for streaming clients.
        try:
            if int(request.headers.get("content-length", "0")) > 2 * 1024 * 1024:
                return JSONResponse({"error": "request_too_large"}, status_code=413)
        except ValueError:
            return JSONResponse({"error": "invalid_request"}, status_code=400)
        response = await call_next(request)
        if request.url.path.startswith(PREFIX):
            response.headers["Cache-Control"] = "no-store"
            response.headers["Referrer-Policy"] = "no-referrer"
        response.headers["X-Content-Type-Options"] = "nosniff"
        return response

    @app.get(PREFIX + "/health")
    def health(identity=node_dependency):
        with store.connection() as db:
            enabled = store.meta(db, "allocation_enabled") == "true"
            revision = int(store.meta(db, 'revision'))
        return {"node_id": identity, "owner_username": getattr(identity, 'owner_username', 'Fuyuki'),
                'revision': revision,
                "allocation_enabled": enabled,
                "gmail_configured": backend is not None and rule is not None}

    @app.post(PREFIX + "/allocations")
    def allocate(body: Allocation, identity=node_dependency):
        return store.allocate(identity, body.model_dump())

    @app.get(PREFIX + "/allocations/{account_id}")
    def allocation(account_id: str, identity=node_dependency):
        return store.allocation(account_id, identity)

    @app.put(PREFIX + "/results/{account_id}")
    def upload(account_id: str, body: Upload, identity=node_dependency):
        validate_final(body.result)
        return auto_publish(store.upload(identity, account_id, body.model_dump()))

    @app.post(PREFIX + "/results")
    def upload_one(body: Upload, identity=node_dependency):
        """Upload one completed account, using the existing durable event receipt."""
        return upload(body.result.account_id, body, identity)

    @app.get(PREFIX + "/results")
    def results(identity=node_dependency):
        value = store.results()
        value["publication"] = publisher.status(value["revision"]) if publisher else {
            "revision": None, "published_at": None, "pending": True}
        return value

    @app.get(PREFIX + "/results/by-index/{index}")
    def result_by_index(index: int = PathParameter(ge=0, le=2**53 - 1), identity=node_dependency):
        return store.result_by_index(index)

    @app.get(PREFIX + "/sync")
    def sync(since: int | None = None, inventory: bool = False, identity=node_dependency):
        value = store.sync_manifest(since=since, inventory=inventory)
        value["publication"] = publisher.status(value["revision"]) if publisher else {"pending": True}
        return value

    @app.post(PREFIX + "/sync/results")
    def sync_results(body: ResultSubset, identity=node_dependency):
        return store.result_subset(body.account_ids)

    @app.get(PREFIX + "/results/{account_id}")
    def result(account_id: str, identity=node_dependency):
        return store.results(account_id)

    @app.post(PREFIX + "/results/{account_id}/used")
    def used(account_id: str, body: Used, identity=node_dependency):
        value = store.used(identity, account_id, body.model_dump())
        # A committed marker stays committed even if publication fails.
        if publisher:
            try:
                value["publication"] = publisher.publish_usage(identity)
            except (OSError, ValueError, Conflict, Unavailable):
                value["publication"] = {"pending": True, "message": "标记成功，网站更新失败；将自动重试。"}
        return value

    @app.get(PREFIX + '/usage/capability')
    def usage_capability(identity=node_dependency):
        return {'usage_protocol': 2, 'statuses': ['available', 'booked', 'used']}

    @app.get(PREFIX + '/results/{account_id}/usage/requests/{request_id}')
    def usage_receipt(account_id: str, request_id: str, identity=node_dependency):
        return store.usage_receipt(identity, account_id, request_id)

    @app.post(PREFIX + '/results/{account_id}/usage')
    def usage(account_id: str, body: Usage, identity=node_dependency):
        value = store.usage(identity, account_id, body.model_dump())
        if publisher:
            try:
                value['publication'] = publisher.publish_usage(identity)
            except (OSError, ValueError, Conflict, Unavailable):
                value['publication'] = {'pending': True, 'message': '状态已保存，网站更新将自动重试。'}
        return value

    @app.get(PREFIX + "/rebind/capability")
    def rebind_capability(identity=node_dependency):
        rebind.authorize(identity)
        return {"enabled": True, "protocol": 2}

    @app.post(PREFIX + "/rebind/tickets")
    def rebind_prepare(body: RebindRequest, identity=node_dependency):
        return rebind.prepare(identity, body.index, body.request_id, body.purpose)

    @app.post(PREFIX + "/rebind/tickets/{ticket}/{action}")
    def rebind_action(ticket: str, action: str, identity=node_dependency):
        return rebind.action(identity, ticket, action)

    @app.post(PREFIX + "/mail/tickets")
    def prepare(body: TicketRequest, identity=node_dependency):
        return tickets.prepare(identity, body.model_dump())

    @app.post(PREFIX + "/mail/tickets/{ticket}/sent")
    def sent(ticket: str, identity=node_dependency):
        return tickets.sent(identity, ticket)

    @app.post(PREFIX + "/mail/tickets/{ticket}/retry")
    def retry_ticket(ticket: str, body: TicketRetry, identity=node_dependency):
        return tickets.retry(identity, ticket, body.model_dump())

    @app.get(PREFIX + "/mail/tickets/{ticket}")
    def poll(ticket: str, identity=node_dependency):
        return tickets.poll(identity, ticket)

    @app.post(PREFIX + "/mail/tickets/{ticket}/complete")
    def complete(ticket: str, identity=node_dependency):
        return tickets.complete(identity, ticket)

    @app.post(PREFIX + "/publications")
    def publish(body: Publish, identity=node_dependency):
        if not publisher:
            raise Unavailable("Publication not configured")
        return publisher.publish(identity, body.model_dump())

    @app.post(PREFIX + "/admin/nodes")
    def create_node(body: NodeRequest, identity=admin_dependency):
        return store.create_node(body.node_id)

    @app.post(PREFIX + "/admin/nodes/{node_id}/revoke")
    def revoke_node(node_id: str, identity=admin_dependency):
        return store.revoke_node(node_id)

    @app.post(PREFIX + "/admin/nodes/{node_id}/rotate")
    def rotate_node(node_id: str, identity=admin_dependency):
        return store.rotate_node(node_id)

    @app.post(PREFIX + "/admin/allocations/verify")
    def verify(body: HighWater, identity=admin_dependency):
        return store.high_water(body.model_dump())

    @app.post(PREFIX + "/admin/allocations/close")
    def close(identity=admin_dependency):
        store.close_allocations()
        return {"allocation_enabled": False}

    @app.post(PREFIX + "/admin/results/import")
    def import_result(body: Historical, identity=admin_dependency):
        validate_final(body.result)
        return store.import_historical(body.model_dump())

    if portal_credential is not None:
        from ournotes.cloud.admin_portal import mount
        mount(app, store, portal_credential, catalog, publisher, presets=presets, activities=activities,
              events=events, manual_mail=manual_mail)

    @app.post(PREFIX + '/admin/oauth')
    def oauth(body: dict, identity=admin_dependency):
        # This maintenance credential exists only in the independent private authorization tool.
        # Browser sessions and ordinary administrator client credentials cannot use this route.
        if not backend or not hasattr(backend, 'import_credentials'):
            raise Unavailable('Gmail backend unavailable')
        return backend.import_credentials(body)

    @app.get("/")
    def public_index():
        if not publisher:
            raise KeyError("index.html")
        return FileResponse(publisher.resource("index.html"), headers={"Cache-Control": "no-cache"})

    @app.get("/{resource:path}")
    def public_resource(resource: str):
        if not publisher:
            raise KeyError(resource)
        headers = {"Cache-Control": "no-cache" if resource == "data/current.json" else "public, max-age=31536000, immutable"}
        return FileResponse(publisher.resource(resource), headers=headers)

    return app
