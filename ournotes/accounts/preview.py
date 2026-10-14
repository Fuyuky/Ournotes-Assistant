"""按账号 index，使用本地 JSON 档案与美术素材生成 SSR/SR 卡牌展示图。"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import re
from collections import Counter
from functools import lru_cache
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

from ournotes.paths import DATA_ROOT as ROOT

INK = "#243450"
MUTED = "#71809c"
BLUE = "#5277c8"
PURPLE = "#9671bc"


@lru_cache(maxsize=128)
def font(size, bold=False):
    name = "msyhbd.ttc" if bold else "msyh.ttc"
    candidates = ([Path("C:/Windows/Fonts") / name] if os.name == "nt" else []) + [
                  Path("/usr/local/share/fonts/microsoft-yahei") / name,
                  Path('/usr/share/fonts/opentype/noto') / ('NotoSansCJK-Bold.ttc' if bold else 'NotoSansCJK-Regular.ttc')]
    for candidate in candidates:
        if candidate.is_file():
            return ImageFont.truetype(str(candidate), size)
    raise ValueError('卡牌展示图缺少中文字体，请配置微软雅黑或安装 Noto Sans CJK。')


def gradient(size, stops):
    w, h = size
    ramp = Image.linear_gradient('L')
    vertical = ramp.resize((w, h))
    horizontal = ramp.rotate(90).resize((w, h))
    field = Image.blend(horizontal, vertical, .5)
    colors = [tuple(bytes.fromhex(c.lstrip('#'))) for c in stops]
    palette = []
    for value in range(256):
        position = value / 255 * (len(colors) - 1)
        left = min(int(position), len(colors) - 2)
        weight = position - left
        palette.extend(round(a * (1 - weight) + b * weight) for a, b in zip(colors[left], colors[left + 1]))
    field.putpalette(palette)
    return field.convert('RGB')


def render(account_path: Path, output: Path, catalog_root=ROOT / "data/catalog/site"):
    raw = account_path.read_bytes()
    account = json.loads(raw)
    manifest = render_account(account, output, catalog_root)
    return {**manifest, 'source': str(account_path), 'source_sha256': hashlib.sha256(raw).hexdigest()}


def render_account(account, output, catalog_root=ROOT / 'data/catalog/site'):
    """Render validated account data to a path or binary stream, without private fields."""
    if account["status"] != "complete" or account.get("unknown_slots", 0):
        raise ValueError("预览要求完整且无未知结果的账号档案。")
    catalog = json.loads((catalog_root / "catalog.json").read_text("utf-8"))
    artwork = {(c["kind"], c["id"]): c for c in catalog["cards"]}
    owned = {}
    for item in account["cards"]:
        card, count = item["card"], item["count"]
        if card["rarity"] not in ("SSR", "SR"):
            continue
        if not isinstance(count, int) or count <= 0:
            raise ValueError("卡牌数量必须为正整数。")
        key = (card["kind"], card["id"])
        ref = artwork[key]
        if ref["rarity"] != card["rarity"]:
            raise ValueError(f"卡牌稀有度与素材目录不一致：{key}")
        owned.setdefault(key, dict(card, title=ref["title"], name=ref["name"], count=0, image=ref["image"]))["count"] += count
    cards = sorted(owned.values(), key=lambda c: (c["kind"], c["rarity"] != "SSR", c["id"]))
    for kind in ("member", "support"):
        for rarity in ("SSR", "SR"):
            actual = sum(c["count"] for c in cards if (c["kind"], c["rarity"]) == (kind, rarity))
            if actual != account["totals"][kind][rarity]:
                raise ValueError(f"档案总数与逐卡明细不符：{kind}/{rarity}")
    groups = {kind: [c for c in cards if c["kind"] == kind] for kind in ("member", "support")}
    totals = Counter()
    for c in cards:
        totals[c["rarity"]] += c["count"]
    width, margin, gap = 1920, 64, 20
    content = width - 2 * margin
    member_w = (content - 6 * gap) // 7
    support_w = (content - 5 * gap) // 6
    member_h = round((member_w - 12) * 4 / 3) + 112
    support_h = round((support_w - 12) * 9 / 16) + 112
    member_rows = math.ceil(len(groups["member"]) / 7)
    support_rows = sum(math.ceil(sum(c["rarity"] == r for c in groups["support"]) / 6) for r in ("SSR", "SR"))
    height = 490 + member_rows * (member_h + gap) + 115 + support_rows * (support_h + 62) + 95
    canvas = gradient((width, height), ["#f7f9ff", "#edf0fc", "#f4f0f9"])
    draw = ImageDraw.Draw(canvas)
    for x in range(0, width, 96):
        draw.line((x, 0, x, height), fill="#e9edf7", width=1)
    for y in range(0, height, 96):
        draw.line((0, y, width, y), fill="#e9edf7", width=1)
    draw.polygon([(1140, 0), (1920, 0), (1920, 280), (1370, 125)], fill="#e7eaf8")
    draw.line((margin, 218, width - margin, 218), fill="#c9d2e9", width=2)
    draw.text((margin, 78), f"账号 {account['alias_index']} · 卡牌一览", font=font(56, True), fill=INK)

    def pill(box, text, fill, color, size=18):
        draw.rounded_rectangle(box, radius=14, fill=fill)
        x, y, r, b = box
        draw.text(((x + r) / 2, (y + b) / 2), text, font=font(size, True), fill=color, anchor="mm")

    for i, (value, label, tint) in enumerate(
        [
            (totals["SSR"], "SSR 总张数", "#725bb3"),
            (totals["SR"], "SR 总张数", "#ad8645"),
        ]
    ):
        x = 1260 + (i + 1) * 196
        draw.rounded_rectangle((x, 56, x + 178, 177), radius=20, fill="#ffffff", outline="#d7def0", width=1)
        draw.text((x + 22, 65), str(value), font=font(45, True), fill=tint)
        draw.text((x + 23, 130), label, font=font(18), fill=MUTED)

    records = []

    def card_tile(c, x, y, w, h):
        is_ssr = c["rarity"] == "SSR"
        draw.rounded_rectangle((x + 3, y + 7, x + w + 3, y + h + 7), radius=18, fill="#dfe4f2")
        mask = Image.new("L", (w, h), 0)
        ImageDraw.Draw(mask).rounded_rectangle((0, 0, w - 1, h - 1), radius=18, fill=255)
        frame = gradient(
            (w, h),
            ["#8c8aec", "#efa5ce", "#f0e996", "#89dfdd"] if is_ssr else ["#c9ab68", "#f1dfad", "#dec19a"],
        )
        canvas.paste(frame, (x, y), mask)
        draw.rounded_rectangle((x + 5, y + 5, x + w - 5, y + h - 5), radius=14, fill="#fbfcff")
        path = catalog_root / c["image"]
        with Image.open(path) as source:
            source = source.convert("RGB")
            art_h = round((w - 12) * source.height / source.width)
            art = source.resize((w - 12, art_h), Image.Resampling.LANCZOS)
        canvas.paste(art, (x + 6, y + 6))
        image_bottom = y + 6 + art_h
        draw.rectangle(
            (x + 6, image_bottom, x + w - 6, image_bottom + 4), fill="#bdb2ef" if is_ssr else "#d4b06f"
        )
        pill((x + 13, y + 13, x + 77, y + 47), c["rarity"], "#f9f8ff", "#755ba6" if is_ssr else "#957139", 17)
        pill((x + w - 76, y + 13, x + w - 13, y + 49), f"×{c['count']}", "#27344e", "#ffffff", 20)
        # Catalog names are canonical; full titles wrap without clipping.
        label_font = font(18, True)
        title_lines = []
        line = ""
        for ch in c["title"]:
            if draw.textlength(line + ch, font=label_font) > w - 30:
                title_lines.append(line)
                line = ch
            else:
                line += ch
        title_lines.append(line)
        if len(title_lines) > 2:
            label_font = font(15, True)
            title_lines = []
            line = ""
            for ch in c["title"]:
                if draw.textlength(line + ch, font=label_font) > w - 30:
                    title_lines.append(line)
                    line = ch
                else:
                    line += ch
            title_lines.append(line)
        if len(title_lines) > 2:
            raise ValueError("卡牌标题超过布局容量，需增加卡片宽度。")
        for j, line in enumerate(title_lines):
            draw.text((x + 15, image_bottom + 15 + j * 25), line, font=label_font, fill=INK)
        name_font = font(17)
        while draw.textlength(c["name"], font=name_font) > w - 30:
            name_font = font(name_font.size - 1)
        draw.text((x + 15, y + h - 38), c["name"], font=name_font, fill=MUTED)
        records.append(
            {
                "kind": c["kind"],
                "id": c["id"],
                "rarity": c["rarity"],
                "count": c["count"],
                "title": c["title"],
                "name": c["name"],
                "box": [x, y, x + w, y + h],
                "source": str(path.relative_to(ROOT) if path.is_relative_to(ROOT) else path.relative_to(catalog_root)),
                "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
            }
        )

    def section(y, number, label, english, kind):
        cs = groups[kind]
        draw.text((margin, y), number, font=font(25), fill="#98a7c6")
        draw.text((margin + 58, y - 6), label, font=font(31, True), fill=INK)
        draw.text((margin + 195, y + 5), english, font=font(16, True), fill=MUTED)
        count = sum(c["count"] for c in cs)
        ssr = sum(c["count"] for c in cs if c["rarity"] == "SSR")
        text = f"{len(cs)} 种卡面 · {count} 张     SSR {ssr} / SR {count - ssr}"
        draw.text((width - margin, y + 9), text, font=font(20), fill=MUTED, anchor="ra")

    y = 254
    section(y, "01", "角色卡", "MEMBER CARDS", "member")
    y += 59
    for i, c in enumerate(groups["member"]):
        card_tile(c, margin + (i % 7) * (member_w + gap), y + (i // 7) * (member_h + gap), member_w, member_h)
    y += member_rows * (member_h + gap) + 48
    section(y, "02", "支援卡", "SUPPORT CARDS", "support")
    y += 58
    for rarity in ("SSR", "SR"):
        cs = [c for c in groups["support"] if c["rarity"] == rarity]
        if not cs:
            continue
        color = PURPLE if rarity == "SSR" else "#a38346"
        draw.text((margin, y), rarity, font=font(20, True), fill=color)
        draw.line((margin + 60, y + 16, width - margin, y + 16), fill="#d0d8eb", width=1)
        y += 42
        for i, c in enumerate(cs):
            card_tile(
                c,
                margin + (i % 6) * (support_w + gap),
                y + (i // 6) * (support_h + gap),
                support_w,
                support_h,
            )
        y += math.ceil(len(cs) / 6) * (support_h + gap) + 15
    y += 14
    canvas = canvas.crop((0, 0, width, y + 72))
    if isinstance(output, Path):
        output.parent.mkdir(parents=True, exist_ok=True)
    canvas.save(output, format='PNG')
    manifest = {
        "index": account["alias_index"],
        "size": list(canvas.size),
        "totals": dict(totals),
        "unique_cards": len(cards),
        "rendered": records,
    }
    return manifest


def find_account(index: int) -> Path:
    folder = ROOT / "results/archive/accounts"
    pattern = re.compile(rf"{index} [0-9]+ SSR\.json")
    matches = sorted(p for p in folder.glob(f"{index} *.json") if pattern.fullmatch(p.name))
    if not matches:
        raise ValueError(f"找不到 index={index} 的账号档案：{folder}")
    if len(matches) != 1:
        raise ValueError(f"index={index} 存在多个账号档案，请先核对：" + "、".join(p.name for p in matches))
    account = json.loads(matches[0].read_text(encoding="utf-8"))
    if account.get("alias_index") != index:
        raise ValueError(f"档案内容的 index 与文件名不一致：{matches[0].name}")
    return matches[0]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("index", type=int, help="账号 index，例如 198")
    args = parser.parse_args()
    if args.index < 0:
        parser.error("index 不能为负数。")
    output = ROOT / "results/previews" / f"{args.index}-卡牌一览.png"
    try:
        render(find_account(args.index), output)
    except (ValueError, OSError) as error:
        parser.error(str(error))
    print(f"已生成：{output}")


if __name__ == "__main__":
    main()
