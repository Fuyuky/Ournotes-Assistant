"""Descriptive SSR statistics, with character identity from the local bdon snapshot."""
from __future__ import annotations

import argparse
import csv
import html
import json
import statistics
from collections import Counter
from pathlib import Path

from ournotes.paths import DATA_ROOT as ROOT
from ournotes.reports.pickups import METHOD, pickup_count, saved_plans
from ournotes.runtime.pools import PRESETS

METRICS = {
    'ssr_total': '单账号 SSR 总张数',
    'mygo_characters': '不同 MyGO SSR 角色数', 'mujica_characters': '不同 Mujica SSR 角色数',
    'unique_characters': '不同 SSR 角色数', 'unique_cards': '不同 SSR 卡面数', 'duplicates': '重复 SSR 张数',
    'dream_characters': '不同梦限大 SSR 角色数',
    'current_up_types': '当期 up 种数',
}
DISPLAY = {p: p for p in PRESETS} | {'MyGO only': 'MyGO', 'Mujica only': 'Mujica', '都抽': '常驻'}
COLORS = ('#326dcc', '#a653b5', '#1b9787')


def account_metrics(account, card_characters, character_bands):
    cards = [c for c in account['summary'] if c['rarity'] == 'SSR']
    characters = {card_characters[c['id']] for c in cards if c['kind'] == 'member'}
    total = sum(account['totals'][k]['SSR'] for k in ('member', 'support'))
    return {'alias_index': account['alias_index'], 'alias': account['alias'], 'preset': account['preset'],
            'instance': account.get('instance', 0), 'status': account['status'],
            'ssr_total': total, 'unique_characters': len(characters),
            'mygo_characters': sum(character_bands[c] == 1 for c in characters),
            'mujica_characters': sum(character_bands[c] == 2 for c in characters),
            'dream_characters': sum(character_bands[c] == 3 for c in characters),
            'pull_count': account.get('pull_count'),
            'current_up_types': pickup_count((f"{c['kind']}:{c['id']}" for c in cards), account.get('plan')),
            'unique_cards': len({(c['kind'], c['id']) for c in cards}),
            'duplicates': total-len({(c['kind'], c['id']) for c in cards}),
            'seconds': account.get('active_seconds', 0)}


def describe(rows):
    result = {}
    for key in METRICS:
        values = [r[key] for r in rows if r[key] is not None]
        if values:
            result[key] = {'n': len(values), 'min': min(values), 'max': max(values),
                           'mean': statistics.mean(values), 'median': statistics.median(values),
                           'histogram': dict(sorted(Counter(values).items()))}
    return result


def analyze(batch, root=ROOT):
    members = json.loads((root/'data/catalog/site/raw/MasterMemberCard.json').read_text(encoding='utf-8'))['_allData']
    characters = json.loads((root/'data/catalog/site/raw/MasterCharacter.json').read_text(encoding='utf-8'))['_allData']
    card_characters = {m['_id']: m['_characterID'] for m in members}
    character_bands = {c['_id']: c['_bandID'] for c in characters}
    all_accounts = []
    for original in batch['accounts']:
        if original.get('archive') and 'summary' not in original:
            compact = json.loads((root/original['archive']).read_text(encoding='utf-8'))
            all_accounts.append({**original, **compact, 'summary': [dict(c['card'], count=c['count'])
                                                                   for c in compact['cards']]})
        else:
            all_accounts.append(original)
    accounts = [a for a in all_accounts if a['status'] == 'complete' and a.get('unknown_slots', 0) == 0]
    if not accounts:
        raise ValueError('没有完整可统计账号')
    if len({a['alias_index'] for a in accounts}) != len(accounts):
        raise ValueError('批次含重复账号，拒绝重复计数')
    plans = saved_plans(root)
    rows = [account_metrics(dict(a, plan=a.get('plan', plans.get(a.get('id', a.get('account_id'))))),
                            card_characters, character_bands) for a in accounts]
    presets = tuple(dict.fromkeys((*PRESETS, *(r['preset'] for r in rows))))
    frequencies = {}
    catalog = json.loads((root/'data/catalog/site/catalog.json').read_text(encoding='utf-8'))['cards']
    for c in catalog:
        if c['rarity'] == 'SSR':
            frequencies[(c['kind'], c['id'])] = {k: c[k] for k in ('id', 'kind', 'title', 'name')}
    for key, entry in frequencies.items():
        entry.update(copies=0, accounts=0, by_preset={p: {'copies': 0, 'accounts': 0} for p in presets})
        for a in accounts:
            count = sum(c['count'] for c in a['summary'] if (c['kind'], c['id']) == key)
            entry['copies'] += count
            entry['accounts'] += bool(count)
            entry['by_preset'][a['preset']]['copies'] += count
            entry['by_preset'][a['preset']]['accounts'] += bool(count)
    groups = {p: describe([r for r in rows if r['preset'] == p]) for p in presets}
    best = {key: [r for r in rows if r[key] == max(x[key] for x in rows)]
            for key in ('ssr_total', 'unique_characters',
                        'mygo_characters', 'mujica_characters', 'dream_characters')}
    eligible = [r for r in rows if r['current_up_types'] is not None]
    if eligible:
        best['current_up_types'] = [r for r in eligible if r['current_up_types'] == max(x['current_up_types'] for x in eligible)]
    return {'batch_id': batch['id'], 'sample_size': len(rows), 'excluded': len(all_accounts)-len(rows),
            'groups': groups, 'overall': describe(rows), 'best': best, 'accounts': rows,
            'cards': sorted(frequencies.values(), key=lambda c: (c['kind'], -c['copies'], c['id'])),
            'method': 'SSR总量按张数；不同角色按MasterMemberCard._characterID去重；MyGO bandID=1，Mujica bandID=2，梦限大 bandID=3；支援卡不计入角色数。不同抽数预设的原始数量不代表账号稀有度，样本频次不等于真实抽取概率。' + METHOD}


def render(report, folder):
    import matplotlib
    matplotlib.use('Agg')
    import matplotlib.pyplot as plt
    import numpy as np
    from matplotlib.ticker import MaxNLocator
    plt.rcParams.update({'font.family': ['Microsoft YaHei', 'Segoe UI Symbol', 'DejaVu Sans'],
                         'axes.unicode_minus': False, 'axes.spines.top': False,
                         'axes.spines.right': False, 'figure.facecolor': '#f7f9fc',
                         'axes.facecolor': '#ffffff', 'savefig.facecolor': '#f7f9fc', 'font.size': 11})
    folder.mkdir(parents=True, exist_ok=True)
    rows = report['accounts']
    files = []

    def save(fig, name):
        fig.savefig(folder/f'{name}.png', dpi=160, bbox_inches='tight')
        fig.savefig(folder/f'{name}.svg', bbox_inches='tight')
        plt.close(fig)
        files.append(name)

    fig, axes = plt.subplots(2, 4, figsize=(20, 9))
    for ax, key in zip(axes.flat, list(METRICS)):
        values = [r[key] for r in rows if r[key] is not None]
        if not values:
            ax.set_title(METRICS[key] + '（无适用样本）')
            ax.set_axis_off()
            continue
        edges = np.arange(-.5, max(values)+1.5, 1)
        counts, _edges, patches = ax.hist(values, bins=edges, color='#326dcc', edgecolor='white', rwidth=.86)
        for n, patch in zip(counts, patches):
            if n:
                ax.text(patch.get_x()+patch.get_width()/2, n+.3, str(int(n)), ha='center', fontsize=9)
        ax.set(title=METRICS[key], xlabel='每账号数量', ylabel='账号数', xticks=range(max(values)+1))
        ax.set_ylim(0, max(counts)*1.18+1)
        ax.yaxis.set_major_locator(MaxNLocator(integer=True))
        ax.grid(axis='y', alpha=.15)
    fig.suptitle(f"{report['sample_size']} 个账号 · SSR 分布", fontsize=20)
    fig.tight_layout(rect=(0, 0, 1, .94))
    save(fig, '01-SSR与角色数量分布')

    fig, axes = plt.subplots(2, 4, figsize=(20, 9))
    active_presets = [p for p, group in report['groups'].items() if group]
    width = .8 / len(active_presets)
    for ax, key in zip(axes.flat, list(METRICS)):
        values = [r[key] for r in rows if r[key] is not None]
        if not values:
            ax.set_title(METRICS[key] + '（无适用样本）')
            ax.set_axis_off()
            continue
        bins = range(max(values)+1)
        for index, preset in enumerate(active_presets):
            selected = [r[key] for r in rows if r['preset'] == preset and r[key] is not None]
            if not selected:
                continue
            count = Counter(selected)
            ax.bar(np.array(list(bins))+(index-(len(active_presets)-1)/2)*width,
                   [100*count[x]/len(selected) for x in bins], width=width*.95,
                   label=f'{DISPLAY.get(preset, preset)} (n={len(selected)})', color=COLORS[index % len(COLORS)])
        ax.set(title=METRICS[key], xlabel='每账号数量', ylabel='组内账号占比（%）', xticks=list(bins))
        ax.grid(axis='y', alpha=.15)
        ax.legend(fontsize=8)
    fig.suptitle('按预设比较 · 使用组内比例比较不同样本量', fontsize=17)
    fig.tight_layout(rect=(0, 0, 1, .94))
    save(fig, '02-各预设分布对比')

    fig, axes = plt.subplots(1, 2, figsize=(17, 7))
    for ax, kind, label in zip(axes, ('member', 'support'), ('角色 SSR', '支援 SSR')):
        selected = sorted((c for c in report['cards'] if c['kind'] == kind), key=lambda c: c['copies'])
        labels = [f"{c['name']}\n【{c['title']}】" for c in selected]
        bars = ax.barh(labels, [c['copies'] for c in selected], color='#326dcc' if kind == 'member' else '#a653b5')
        ax.bar_label(bars, labels=[f"{c['copies']} 张 / {c['accounts']} 账号" for c in selected], padding=4, fontsize=9)
        ax.set(title=label+' · 样本出现频次', xlabel='获得张数')
        ax.set_xlim(0, max(c['copies'] for c in selected)*1.4+1)
        ax.tick_params(axis='y', labelsize=9)
    fig.suptitle('本次最常见 / 最少见 SSR · 频次受卡池、UP、保底和样本量影响', fontsize=16)
    fig.tight_layout(rect=(0, 0, 1, .94))
    save(fig, '03-SSR卡牌频次')

    fig, axes = plt.subplots(1, 3, figsize=(16, 4.8))
    for ax, key in zip(axes[:2], ('unique_cards', 'duplicates')):
        values = [r[key] for r in rows if r[key] is not None]
        ax.hist(values, bins=np.arange(-.5, max(values)+1.5, 1), rwidth=.85, color='#1b9787')
        ax.set(title=METRICS[key], xlabel='每账号数量', ylabel='账号数', xticks=range(max(values)+1))
        ax.yaxis.set_major_locator(MaxNLocator(integer=True))
    pairs = Counter((r['mygo_characters'], r['mujica_characters']) for r in rows)
    for (x, y), count in pairs.items():
        axes[2].scatter(x, y, s=70+count*55, color='#326dcc', alpha=.65)
        axes[2].text(x, y, str(count), ha='center', va='center', color='white', fontsize=9)
    axes[2].set(title='MyGO / Mujica 角色组合（数字为账号数）', xlabel='MyGO SSR 角色数', ylabel='Mujica SSR 角色数')
    axes[2].set_xticks(range(max(r['mygo_characters'] for r in rows)+1))
    axes[2].set_yticks(range(max(r['mujica_characters'] for r in rows)+1))
    fig.tight_layout()
    save(fig, '04-重复卡与乐团组合')
    return files


def build(batch_path, root=ROOT):
    batch = json.loads(batch_path.read_text(encoding='utf-8'))
    report = analyze(batch, root)
    folder = batch_path.parent/'statistics'
    folder.mkdir(parents=True, exist_ok=True)
    (folder/'statistics.json').write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')
    with (folder/'accounts.csv').open('w', encoding='utf-8-sig', newline='') as f:
        writer = csv.DictWriter(f, fieldnames=list(report['accounts'][0]))
        writer.writeheader()
        writer.writerows(report['accounts'])
    files = render(report, folder)
    lines = [f"# {report['sample_size']} 账号长测统计", '', report['method'], '',
             f"仅统计本批完整且无未知结果的账号；排除 {report['excluded']} 个。", '',
             '| 预设 | 账号数 | SSR均值 | 中位数 | 最少 / 最多 |',
             '|---|---:|---:|---:|---|']
    for preset, group in report['groups'].items():
        if not group:
            continue
        d = group['ssr_total']
        lines.append(f"| {DISPLAY.get(preset, preset)} | {d['n']} | {d['mean']:.2f} | {d['median']} | {d['min']} / {d['max']} |")
    lines += ['', '## 单账号最高记录', '']
    for key, selected in report['best'].items():
        lines.append(f"- {METRICS[key]}最高 **{selected[0][key]}**：" + '；'.join(
            f"编号 {r['alias_index']}（{r['ssr_total']} SSR）" for r in selected))
    lines += ['', '## 本样本最常见与最少见 SSR', '']
    for kind, label in [('member', '角色'), ('support', '支援')]:
        observed = [c for c in report['cards'] if c['kind'] == kind and c['copies'] > 0]
        if not observed:
            lines.append(f'- 本次没有获得{label} SSR。')
            continue
        for word, number in [('最多', max(c['copies'] for c in observed)), ('最少（已出现）', min(c['copies'] for c in observed))]:
            for c in observed:
                if c['copies'] == number:
                    lines.append(f"- {label} SSR {word}：【{c['title']}】{c['name']}，{c['copies']} 张，出现在 {c['accounts']} 个账号。")
        unseen = [c for c in report['cards'] if c['kind'] == kind and c['copies'] == 0]
        if unseen:
            lines.append(f"- {label} SSR 在当前素材目录中但本次未出现：" + '、'.join(c['name']+'【'+c['title']+'】' for c in unseen))
    lines += ['', '这些是本次样本频次，不是游戏真实出率。不同预设含不同UP池、票池和保底，组内比例仅用于描述账号结果。', '']
    for name in files:
        lines += [f'![{name}]({name}.png)', '']
    (folder/'报告.md').write_text('\n'.join(lines), encoding='utf-8')
    cards = ''.join(f'<section><h2>{html.escape(name[3:])}</h2><img src="{name}.png"></section>' for name in files)
    table_rows = ''.join(f"<tr><td>{r['alias_index']}</td><td>{html.escape(r['alias'])}</td><td>{html.escape(DISPLAY.get(r['preset'], r['preset']))}</td>"
                         f"<td>{r['ssr_total']}</td>"
                         f"<td>{r['mygo_characters']}</td><td>{r['mujica_characters']}</td><td>{r['unique_characters']}</td><td>{r['current_up_types'] if r['current_up_types'] is not None else '—'}</td></tr>"
                         for r in sorted(report['accounts'], key=lambda r: (-r['ssr_total'], r['alias_index'])))
    content = f'''<!doctype html><html lang="zh-CN"><meta charset="utf-8"><title>长测 SSR 统计</title>
<style>body{{font:16px 'Microsoft YaHei',sans-serif;background:#f7f9fc;color:#15243a;max-width:1500px;margin:40px auto;padding:0 24px}}section{{background:white;padding:20px;margin:24px 0;border-radius:12px}}img{{width:100%}}table{{border-collapse:collapse;width:100%;font-size:13px}}td,th{{padding:9px;border-bottom:1px solid #e2e8f0;text-align:left}}th{{position:sticky;top:0;background:#eaf0f8}}</style>
<h1>{report['sample_size']} 账号 · SSR 长测统计</h1><p>按实际预设分组比较。不同角色按角色ID去重；支援卡不计入角色数。不同抽数预设的原始数量不代表账号稀有度。</p>
<p>当前展示的是样本结果，不代表卡池真实出率。<a href="报告.md">完整文字报告</a> · <a href="accounts.csv">逐账号CSV</a> · <a href="statistics.json">统计JSON</a></p>
{cards}<section><h2>按单账号 SSR 数量排序</h2><table><thead><tr><th>编号</th><th>邮箱</th><th>预设</th><th>SSR合计</th><th>MyGO角色</th><th>Mujica角色</th><th>不同角色</th><th>当期 up 种数</th></tr></thead><tbody>{table_rows}</tbody></table></section></html>'''
    (folder/'报告.html').write_text(content, encoding='utf-8')
    return report


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('batch', type=Path)
    args = parser.parse_args()
    result = build(args.batch)
    print(json.dumps({'sample_size': result['sample_size'], 'folder': str(args.batch.parent/'statistics')}, ensure_ascii=False))


if __name__ == '__main__':
    main()
