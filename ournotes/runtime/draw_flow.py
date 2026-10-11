"""Persist draw intent, capture evidence and consume recognition results."""
from __future__ import annotations

import hashlib
import re
import time
import uuid
from typing import TYPE_CHECKING

import numpy as np

from ournotes.runtime.model import PauseRun
from ournotes.runtime.pools import validate_confirmation
from ournotes.vision.recognition import RecognitionError, artwork_box

if TYPE_CHECKING:
    from ournotes.runtime.runner import Runner


class DrawFlow:
    def __init__(self, runner: Runner):
        self.runner = runner

    def current_draw(self):
        r = self.runner
        draws = r.db.draws(r.account["id"])
        pending = [d for d in draws if d["status"] in ("planned", "submitted")]
        if pending:
            return pending[-1]
        if r.pause_reason or (r.stop and r.stop.is_set()):
            raise PauseRun('暂停期间禁止创建新抽卡交易。')
        from ournotes.runtime.draw_strategy import redirect_after_pickups
        redirected = redirect_after_pickups(r.account['plan'], draws, r.account['id'])
        if redirected:
            plan, audit = redirected
            r.account['plan'] = plan
            r.account.setdefault('plan_changes', []).append(dict(audit, time=time.time()))
            r.save()
            r.log.event('pickup_targets_complete', **audit)
        ordinal = len(draws)
        if ordinal >= len(r.account["plan"]):
            return None
        draw = dict(id=uuid.uuid4().hex, account_id=r.account["id"], ordinal=ordinal,
                    status="planned", created_at=time.time(), **r.account["plan"][ordinal])
        draw['recognition_source'] = 'settlement'
        r.db.save_draw(draw)
        return draw


    def record_post_gifts_balance(self, draw):
        r = self.runner
        if r.account.get('post_gifts_balance') or draw['ordinal'] != 0:
            return True
        r.read_fields('wallet', draw['id'])
        numbers = []
        for bounds in ((1090, 35, 1155, 68), (1320, 35, 1455, 68)):
            values = {int(t.text.replace(',', '')) for t in r.observation.texts
                      if t.box == bounds and re.fullmatch(r'[0-9,]+', t.text)}
            if len(values) != 1:
                r.ctx['wallet_read_failures'] = r.ctx.get('wallet_read_failures', 0) + 1
                if r.ctx['wallet_read_failures'] >= 3:
                    raise PauseRun('领完邮件后的星钻余额无法完整读取；尚未提交抽卡。')
                return False
            numbers.append(values.pop())
        fingerprint = (r.account['id'], *numbers)
        previous = r.wallet_observation
        r.wallet_observation = (fingerprint, time.monotonic())
        if not previous or previous[0] != fingerprint:
            return False
        r.account['post_gifts_balance'] = {
            'paid_stars': numbers[0], 'free_stars': numbers[1], 'observed_at': time.time(),
            'source': 'pool_wallet_before_first_draw',
            'roi_sha256': hashlib.sha256(r.observation.image.crop((910, 12, 1470, 83)).tobytes()).hexdigest(),
        }
        r.save()
        r.log.event('post_gifts_balance', account=r.account['id'], **r.account['post_gifts_balance'])
        return True


    def skip_draw(self, draw, reason):
        r = self.runner
        draw.update(status="skipped", reason=reason)
        r.db.save_draw(draw)
        r.account["skipped"].append({"ordinal": draw["ordinal"], "pool_key": draw["pool_key"], "reason": reason})
        r.ctx.pop("scan", None)
        r.last_action = None
        r.save()
        r.db.export(r.account["id"])


    def draws(self):
        r = self.runner
        o = r.observation
        if o.scene != 'pool':
            r.pool_navigation.invalidate()
        if o.scene == 'title':
            if any(d['status'] == 'submitted' for d in r.db.draws(r.account['id'])):
                r.abandon_submission('已提交交易的结算页在返回标题后丢失；放弃账号，禁止自动补抽。')
                return
            # Closing the emulator does not preserve navigation. Verify the same
            # UID before resuming the existing plan; never allocate another alias.
            r.ctx['uid_verified'] = False
            r.ctx['gifts_done'] = True
            r.phase('rewards')
            return
        if o.scene == 'notice':
            # Announcements may arrive after leaving the inbox or entering a pool.
            # Dismiss only the modal; never advance or recreate a draw transaction.
            r.click('关闭', exact=True)
            return
        if o.scene == 'startup_reward' and not r.submitted_draws():
            r.reward_flow.dismiss_reward()
            return
        if o.scene == 'home' and not r.submitted_draws():
            # Startup rewards or an application restart may return to the home page.
            r.click('招募', exact=True)
            return
        finished = [d for d in r.db.draws(r.account["id"])
                    if d["status"] in ("captured", "complete", "needs_review") and not d.get("result_exit_confirmed")]
        if finished:
            previous = finished[-1]
            if o.scene == "draw_result":
                if (o.result_layout or {}).get('top_state') == 'pressed':
                    if not previous.get('result_top_release_attempted'):
                        previous['result_top_release_attempted'] = True
                        r.db.save_draw(previous)
                        r.action('release-result-top-touch', kind='release_touch')
                    return
                r.click("招募TOP")
                return
            if o.scene == 'home' and not r.submitted_draws():
                # After an app restart, return to the pool before confirming the
                # already captured result was left; never create another draw here.
                r.click('招募', exact=True)
                return
            if o.scene != "pool":
                return
            previous["result_exit_confirmed"] = True
            r.db.save_draw(previous)
            r.ctx.pop("result_to_leave", None)
        if o.scene == "home":
            r.click("招募", exact=True)
            return
        draw = self.current_draw()
        if draw is None:
            r.db.export(r.account["id"])
            r.phase("logout")
            return
        if o.scene == "draw_result":
            if draw["status"] != "submitted":
                raise PauseRun("出现不属于当前已提交交易的结算页。")
            if r.same_scene < 2:
                return
            try:
                positions, _ = r.layout.inspect(o.image, foreground=True)
            except RecognitionError:
                return
            if len(positions) != draw["pulls"]:
                return
            pixels = np.concatenate([np.asarray(o.image.crop(artwork_box(x)).resize((26, 45)), dtype=float)
                                     for x in positions])
            previous = r.result_stability
            r.result_stability = (draw["id"], pixels)
            if not previous or previous[0] != draw["id"] or float(np.abs(previous[1] - pixels).mean()) > 6:
                return
            with r.log.timed("result_save", draw_id=draw["id"]):
                draw["screenshot"] = r.log.screenshot(o.image, "result-" + draw["id"], persist=True)
            draw.update(status="captured", captured_at=time.time(), result_exit_confirmed=False)
            r.db.save_draw(draw)
            r.log.event("result_captured", draw_id=draw["id"], ordinal=draw["ordinal"], pulls=draw["pulls"])
            r.recognition.submit(draw)
            r.result_stability = None
            r.click("招募TOP")
            return
        if o.scene == "animation":
            if draw["status"] != "submitted":
                raise PauseRun("出现未记录的抽卡演出。")
            if o.find("SKIP"):
                r.click("SKIP")
            else:
                r.action("skip", args=(1484, 62))
            return
        if o.scene == "draw_confirm":
            if draw["status"] == "submitted":
                # A lost touch-up can leave the original confirmation pressed.
                # Release once, without any new down/click or transaction submission.
                if (not draw.get('release_recovery_attempted')
                        and time.time() - draw.get('submitted_at', time.time()) >= 3):
                    draw['release_recovery_attempted'] = True
                    r.db.save_draw(draw)
                    r.action('release-confirm-touch', kind='release_touch')
                return  # Never repeat an uncertain confirmation.
            if not o.find("OK", exact=True):
                return
            if not r.refresh_draw_confirmation():
                return
            o = r.observation
            r.read_fields("confirmation", draw["id"])
            evidence = validate_confirmation(o, draw, r.pools.catalog)
            r.check_pause()
            r.check_memory()
            draw.update(status="submitted", confirmation=evidence,
                        before=r.log.screenshot(o.image, "confirmation-" + draw["id"]),
                        submitted_at=time.time())
            # A soft stop arriving between durable intent and input must not
            # suppress this one committed click and strand an unsent transaction.
            r.committing_draw_id = draw['id']
            try:
                r.db.save_draw(draw)
                r.draw_wait_started = (draw['id'], time.monotonic())
                r.click("OK", exact=True)
            finally:
                r.committing_draw_id = None
            return
        if o.scene != "pool":
            return
        if draw["status"] == "submitted":
            # Closing the confirmation can expose the old pool for a frame
            # before the server response starts the animation. Observe only;
            # the progress watchdog bounds this without a second submission.
            return
        with r.log.timed('pool_identity'):
            current, positions, _scores = r.pools.scan(o.image)
        key = draw["pool_key"]
        decision = r.pool_navigation.observe(r.ctx, draw['id'], key, current, positions, time.time())
        if decision == 'wait':
            return
        if decision == 'ready':
            if not self.record_post_gifts_balance(draw):
                return
            # Observe resource shortage by red cost digits, never by neighboring pool position.
            x = 1447 if draw["pulls"] == 10 else 1150
            roi = np.asarray(o.image)[727:763, x + 10:x + 105]
            red = ((roi[:, :, 0] > 170) & (roi[:, :, 0] > roi[:, :, 1] * 1.3)
                   & (roi[:, :, 0] > roi[:, :, 2] * 1.2)).mean()
            if red > .025:
                self.skip_draw(draw, "资源不足")
                return
            if draw["pool_key"] == "our-notes-standard" and not any(
                    "/" in t.text and 1200 < t.center[0] < 1580 and 600 < t.center[1] < 805
                    for t in o.texts):
                # The confirmation remains the final guard if the ticket counter OCR is absent.
                pass
            self.open_draw_confirmation(draw)
            return
        if decision == 'select':
            attempt_key = (draw["id"], key)
            attempts = r.pool_selection_attempts.get(attempt_key, 0)
            if attempts >= 3:
                raise PauseRun("卡池选择三次后仍未稳定到目标页面。")
            if r.action("select-" + key, args=positions[key]["center"], force=True):
                r.pool_selection_attempts[attempt_key] = attempts + 1
                r.pool_navigation.invalidate()
            return
        if decision == 'missing':
            r.log.event('pool_scan_complete', draw_id=draw['id'], pool_key=key, scan=dict(r.ctx['scan']))
            self.skip_draw(draw, '两端各经重复滑动核实，完整浏览后未发现目标卡池')
            return
        if decision == 'uncertain':
            raise PauseRun('卡池扫描达到次数上限，未确认完整浏览两端；保留抽卡计划，禁止推断卡池已失效。')
        direction = r.ctx['scan']['direction']
        a, b = ((175, 250), (175, 750)) if direction == 'up' else ((175, 750), (175, 250))
        if r.action('scan-' + direction, kind='swipe', args=(a, b), force=True):
            r.pool_navigation.sent(r.ctx, current, positions, time.time())
            r.save()
            r.log.event('pool_scan_swipe', draw_id=draw['id'], direction=direction,
                           swipes=r.ctx['scan']['swipes'], viewport=r.ctx['scan']['pending']['before'])


    def open_draw_confirmation(self, draw):
        """Called only after PoolNavigator confirms the planned pool is stable."""
        r = self.runner
        if draw['status'] != 'planned':
            return
        o = r.observation
        value = '招募' + str(draw['pulls']) + '次'
        region = (1300, 650, 1595, 780) if draw['pulls'] == 10 else (1030, 650, 1295, 780)
        r.draw_button_ocr_target = None
        if not o.find(value, exact=True, region=region):
            now = time.monotonic()
            budget = r.ctx.setdefault('draw_button_ocr', {})
            record = budget.setdefault(draw['id'], {'attempts': 0, 'last_at': 0})
            if record['attempts'] >= 3:
                raise PauseRun('目标卡池已确认，但招募按钮局部识别已达三次上限；未提交交易。')
            # Durable count, monotonic cooldown only within this process.
            cooldown = r.last_button_ocr
            if now - cooldown.get(draw['id'], float('-inf')) < 2:
                return
            record['attempts'] += 1
            record['last_at'] = time.time()
            r.save()
            r.last_button_ocr = {draw['id']: now}
            r.read_fields('draw_button_' + str(draw['pulls']), draw['id'])
            target = o.find(value, exact=True, region=region)
            if not target:
                return
            r.draw_button_ocr_target = target.box
        r.click(value, exact=True, region=region, label='open-draw-confirmation')


    def check_recognition(self, schedule=True):
        r = self.runner
        if not r.account:
            return
        draws = {d["id"]: d for d in r.db.draws(r.account["id"])}
        if schedule:
            for draw in draws.values():
                if draw["status"] == "captured":
                    r.recognition.submit(draw)
        for draw_id, value in r.recognition.completed():
            if draw_id not in draws:
                # A retired account's worker may finish later; its durable cache remains evidence.
                r.log.event('retired_recognition_cached', draw_id=draw_id)
                continue
            draw = draws[draw_id]
            draw.update(status=value["status"], result=value.get("result", {}),
                        completed_at=value["finished_at"], recognition_timing={k: v for k, v in value.items()
                                                                         if k != "result"})
            r.db.save_draw(draw)
            r.log.event("recognition_finished", draw_id=draw_id, ordinal=draw["ordinal"],
                           pulls=draw["pulls"], **{k: v for k, v in value.items() if k not in ("result", "draw_id")})
            r.db.export(r.account["id"])
