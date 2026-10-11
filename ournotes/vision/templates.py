"""Reviewed native-pixel crops from recognition incidents, never online self-labels."""
from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path

from PIL import Image

TEMPLATE_DIRECTORY = Path(__file__).with_name('assets') / 'settlement-templates'


def load_templates(kind, directory=None):
    folder = Path(directory) if directory is not None else TEMPLATE_DIRECTORY
    manifest = json.loads((folder / 'manifest.json').read_text('utf-8'))
    if manifest.get('schema_version') != 1:
        raise ValueError('不支持的失败截图素材清单。')
    loaded, seen = [], set()
    for spec in manifest['templates']:
        if spec.get('kind') != kind:
            continue
        if spec.get('verified') is not True or not spec.get('verification'):
            raise ValueError('失败截图素材尚未核验。')
        identity = spec.get('key') if kind == 'card' else spec.get('id')
        if not identity or identity in seen:
            raise ValueError('失败截图素材身份重复或缺失。')
        seen.add(identity)
        if not re.fullmatch(r'[a-f0-9]{64}', spec.get('source_sha256', '')):
            raise ValueError('失败截图缺少有效来源哈希。')
        if kind == 'card' and not re.fullmatch(r'[a-f0-9]{64}', spec.get('catalog_card_sha256', '')):
            raise ValueError('卡牌截图缺少核验时的卡牌素材哈希。')
        path = (folder / spec['file']).resolve()
        if not path.is_relative_to(folder.resolve()):
            raise ValueError('失败截图素材路径超出目录。')
        if hashlib.sha256(path.read_bytes()).hexdigest() != spec['sha256']:
            raise ValueError('失败截图素材哈希不匹配。')
        x1, y1, x2, y2 = spec['box']
        width, height = spec['source_size']
        if not (0 <= x1 < x2 <= width and 0 <= y1 < y2 <= height):
            raise ValueError('失败截图素材裁切范围无效。')
        with Image.open(path) as image:
            if image.size != (x2 - x1, y2 - y1):
                raise ValueError('失败截图素材必须保留原生裁切尺寸。')
            loaded.append((spec, image.convert('RGB')))
    return loaded


def provenance(spec):
    return {k: spec[k] for k in ('id', 'source_sha256', 'sha256', 'box')}
