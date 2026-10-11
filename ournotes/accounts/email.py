"""Resolve a private email from actual account artifacts only."""
import json
from pathlib import Path

from ournotes.paths import DATA_ROOT


def find_email(index, root=DATA_ROOT):
    if type(index) is not int or index < 0:
        raise ValueError('index 必须为非负整数。')
    matches = []
    for path in (Path(root) / 'results/archive/accounts').glob(f'{index} *.json'):
        value = json.loads(path.read_text('utf-8'))
        if value['alias_index'] == index:
            matches.append(value['alias'])
    if len(matches) != 1:
        raise ValueError('找不到唯一的现存账号档案。')
    return matches[0]
