"""Build a wall-clock step ledger and non-overlapping foreground cost breakdown."""
from __future__ import annotations

import argparse
import json
from bisect import bisect_left, bisect_right
from collections import defaultdict
from pathlib import Path

from ournotes.common.files import atomic_json, atomic_json_gzip
from ournotes.paths import DATA_ROOT as ROOT

PHASES = {"switch_account": "切换账号及清理登录记录", "login": "注册与验证码", "rewards": "领取开屏奖励",
          "gifts": "领取邮件奖励", "draws": "招募", "logout": "退出与识别收尾", "finished": "完成"}
LABELS = {"expand-login-history": "展开登录记录", "remove-login-history": "删除一条登录记录",
          "confirm-remove-login-history": "确认删除登录记录", "focus-email": "聚焦邮箱输入框",
          "fill-email": "填入邮箱别名", "accept-game-terms": "勾选游戏协议", "focus-code": "聚焦验证码框",
          "fill-code": "填入验证码", "verify-account-menu": "打开菜单核对账号", "close-user-center": "关闭用户中心",
          "TAP TO START": "进入游戏", "advance-reward": "领取开屏奖励", "dismiss-reward-OK": "确认开屏奖励",
          "open-gifts": "打开邮件", "claim-gifts": "确认领取邮件奖励", "ack-gifts": "确认领取结果",
          "open-draw-confirmation": "打开抽卡确认框", "SKIP": "跳过抽卡演出", "skip": "跳过抽卡演出",
          "pool-menu": "打开招募页菜单", "title-menu": "打开标题页菜单", "home-menu": "打开主界面菜单"}
TOP = {"startup_emulator", "startup_sdk", "startup_vision", "background_collect", "capture", "vision",
       "frame_history", "publish", "step", "checkpoint", "recognition_drain"}


def load_lines(path):
    from ournotes.runtime.storage import read_lines
    return read_lines(path)


def lane_report(root, lane):
    folder = root / "state/runs" / lane["run_id"]
    events, metrics = load_lines(folder / "events.jsonl"), load_lines(folder / "timings.jsonl")
    if not events or not metrics:
        return None
    metrics.sort(key=lambda m: m['started_at'])
    metric_times = [m['started_at'] for m in metrics]
    begin = next(e["time"] for e in events if e["event"] == "run_started")
    end = next(e["time"] for e in reversed(events) if e["event"] == "run_finished")
    phase = "switch_account"
    actions = []
    intents = {}
    for event in events:
        if event["event"] == "phase":
            phase = event["phase"]
        if event["event"] == "action_intent":
            item = dict(event, phase=phase)
            actions.append(item)
            intents[event["action_id"]] = item
    sessions = []
    started = None
    for event in events:
        if event['event'] == 'run_started':
            started = event['time']
        elif event['event'] == 'run_finished' and started is not None:
            sessions.append((started, event['time']))
            started = None
    steps, offset = [], 0.0
    for session_index, (start, finish) in enumerate(sessions):
        own_metrics = [m for m in metrics if start <= m['started_at'] <= finish]
        startup_end = max((m['started_at'] + m['seconds'] for m in own_metrics
                           if m['operation'].startswith('startup_')), default=start)
        endpoints = [{'time': startup_end, 'label': '初始化模拟器与页面识别', 'phase': 'startup'}]
        for event in events:
            if event['event'] == 'action_finished' and start <= event['time'] <= finish:
                intent = intents[event['action_id']]
                endpoints.append({'time': event['time'], 'label': LABELS.get(event['label'], event['label']),
                                  'phase': intent['phase']})
        endpoints.append({'time': finish, 'label': '账号与后台任务收尾', 'phase': 'finished'})
        previous = start
        for event in endpoints:
            until = event['time']
            own = metrics[bisect_left(metric_times, previous):bisect_left(metric_times, until)]
            def elapsed(op, measures=own):
                return sum(m['seconds'] for m in measures if m['operation'] == op)
            steps.append({'index': len(steps), 'session': session_index, 'start_offset': offset+previous-start,
                          'seconds': until-previous, 'label': event['label'], 'phase': event['phase'],
                          'vision_seconds': elapsed('vision'), 'field_ocr_seconds': elapsed('field_ocr'),
                          'capture_seconds': elapsed('capture')+elapsed('action_guard_capture'),
                          'input_seconds': elapsed('device_input'),
                          'evidence_seconds': elapsed('action_evidence')+elapsed('result_save')})
            previous = until
        offset += finish-start
    totals, vision_modes = defaultdict(float), defaultdict(lambda: {"calls": 0, "seconds": 0.0})
    for metric in metrics:
        totals[metric["operation"]] += metric["seconds"]
        if metric["operation"] == "vision":
            mode = metric["mode"]
            vision_modes[mode]["calls"] += 1
            vision_modes[mode]["seconds"] += metric["seconds"]
    recognition = [e for e in events if e["event"] == "recognition_finished"]
    jobs_by_draw = {e['draw_id']: e for e in recognition}
    scenes = sorted(e['time'] for e in events if e['event'] == 'scene' and e.get('scene') == 'draw_result')
    top_actions = sorted((a for a in actions if a['label'] == '招募TOP'), key=lambda a: a['time'])
    top_times = [a['time'] for a in top_actions]
    dwell = []
    for e in events:
        if e["event"] != "result_captured":
            continue
        index = bisect_right(scenes, e['time']) - 1
        scene = scenes[index] if index >= 0 else e['time']
        index = bisect_left(top_times, e['time'])
        click = top_actions[index] if index < len(top_actions) else None
        job = jobs_by_draw.get(e['draw_id'])
        if click:
            dwell.append({"ordinal": e["ordinal"], "pulls": e["pulls"], "seconds": click["time"] - scene,
                              "captured_to_click": click["time"] - e["time"],
                              "clicked_before_recognition_done": bool(job and click["time"] < job["finished_at"])})
    return dict(lane, wall_seconds=offset, calendar_seconds=end-begin, paused_seconds=end-begin-offset,
                sessions=len(sessions), steps=steps, operation_seconds=dict(totals),
                foreground_accounted_seconds=sum(v for k, v in totals.items() if k in TOP),
                vision_modes=dict(vision_modes), recognition=recognition, result_dwell=dwell,
                mail=[e for e in events if e["event"] == "mail_timing"])


def build(batch, root=ROOT):
    folder = root / "results/batches" / batch["id"]
    summaries = []
    for lane in batch['lanes']:
        source = root / 'state/runs' / lane['run_id'] / 'timing-summary.json'
        summaries.append({**lane, 'timings': json.loads(source.read_text('utf-8')) if source.exists() else {}})
    atomic_json(folder / 'timing-summary.json', {'batch_id': batch['id'], 'lanes': summaries,
                                               'active_seconds': batch.get('active_seconds'),
                                               'shutdown': batch.get('shutdown')})
    if not batch.get('debug_mode', False):
        return
    lanes = [r for lane in batch["lanes"] if (r := lane_report(root, lane))]
    if not lanes:
        return
    report = {"batch_id": batch["id"], "lanes": lanes, "shutdown": batch.get("shutdown")}
    atomic_json_gzip(folder / "performance.json.gz", report)
    (folder / 'performance.json').unlink(missing_ok=True)
    selected = lanes[0]
    lines = ["# 多开分步骤耗时记录", "", "单位：秒。后台识别和邮件访问与前台重叠，不能与前台墙钟时间相加。",
             "每行从上一步结束，到本动作完成；包含等待界面、观察、截图留证和本次输入。初始化和末尾收尾也计入，所有步骤时间相加等于该实例的运行时间。", "",
             "| 实例 | 预设 | 流程用时 | 卡片识别计算总耗时（后台） | OCR/模板观察总耗时 |",
             "|---|---|---:|---:|---:|"]
    for lane in lanes:
        lines.append(f"| {lane['instance']} | {lane['preset']} | {lane['wall_seconds']:.3f} | "
                     f"{sum(j.get('recognition_seconds', 0) for j in lane['recognition']):.3f} | "
                     f"{lane['operation_seconds'].get('vision', 0):.3f} |")
    lines += ["", f"## 实例 {selected['instance']} · {selected['preset']} 完整动作记录", "",
              "| 序号 | 开始于 | 阶段 | 动作／步骤 | 本步耗时 | 页面观察 | 字段OCR | 截图 | 输入 | 保存图像 |",
              "|---:|---:|---|---|---:|---:|---:|---:|---:|---:|"]
    for s in selected["steps"]:
        lines.append(f"| {s['index']} | {s['start_offset']:.3f} | {PHASES.get(s['phase'], s['phase'])} | "
                     f"{s['label']} | {s['seconds']:.3f} | {s['vision_seconds']:.3f} | {s['field_ocr_seconds']:.3f} | "
                     f"{s['capture_seconds']:.3f} | {s['input_seconds']:.3f} | {s['evidence_seconds']:.3f} |")
    lines += ["", "## 该实例观察方式", "", "| 方式 | 次数 | 总耗时 | 单次均值 |", "|---|---:|---:|---:|"]
    for mode, data in selected["vision_modes"].items():
        lines.append(f"| {mode} | {data['calls']} | {data['seconds']:.3f} | {data['seconds']/data['calls']:.4f} |")
    lines += ["", "## 该实例后台识别", "", "| 第几笔 | 张数 | 排队 | 模型初始化 | 纯识别 | 总任务用时 | 缓存复用 |",
              "|---:|---:|---:|---:|---:|---:|---|"]
    for j in selected["recognition"]:
        lines.append(f"| {j['ordinal']+1} | {j['pulls']} | {j['queue_seconds']:.3f} | {j['model_seconds']:.3f} | "
                     f"{j.get('recognition_seconds',0):.3f} | {j['worker_seconds']:.3f} | {j.get('cached')} |")
    lines += ["", "## 该实例结算停留", "", "| 第几笔 | 张数 | 观察到结算至点击返回 | 截图落盘至点击返回 | 返回时识别尚未完成 |",
              "|---:|---:|---:|---:|---|"]
    for d in selected["result_dwell"]:
        lines.append(f"| {d['ordinal']+1} | {d['pulls']} | {d['seconds']:.3f} | {d['captured_to_click']:.3f} | "
                     f"{d['clicked_before_recognition_done']} |")
    lines += ["", "关机核验：" + json.dumps(batch.get("shutdown"), ensure_ascii=False)]
    (folder / "完整分步骤用时.md").write_text("\n".join(lines), encoding="utf-8")
    return report


def main():
    p = argparse.ArgumentParser()
    p.add_argument("batch", type=Path)
    args = p.parse_args()
    build(json.loads(args.batch.read_text(encoding="utf-8")))


if __name__ == "__main__":
    main()
