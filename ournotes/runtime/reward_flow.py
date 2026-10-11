"""Verify identity and collect available rewards before drawing."""
from __future__ import annotations

import re
from typing import TYPE_CHECKING

from ournotes.runtime.model import PauseRun

if TYPE_CHECKING:
    from ournotes.runtime.runner import Runner


class RewardFlow:
    def __init__(self, runner: Runner):
        self.runner = runner

    def dismiss_reward(self):
        r = self.runner
        if r.observation.find("OK", exact=True):
            r.click("OK", exact=True, label="dismiss-reward-OK")
        else:
            r.action("advance-reward", args=(80, 850))


    def rewards(self):
        r = self.runner
        o = r.observation
        if o.scene == "user_center" and r.ctx.get("uid_verified"):
            r.action("close-user-center", args=(612, 258))
            return
        if not r.ctx.get("uid_verified"):
            if o.scene == "title":
                if r.ctx.get('title_auth_recovery'):
                    r.click('TAP TO START', exact=True)
                else:
                    r.action("verify-account-menu", args=(1541, 58))
                return
            if o.scene in ("title_menu", "home_menu"):
                r.click("用户中心", exact=True)
                return
            if o.scene == 'home':
                r.action('home-menu', args=(1541, 51))
                return
            if o.scene == "user_center":
                r.read_fields("uid")
                uid = re.search(r"UID\s*[:：]?\s*(\d{8,})", o.text, re.IGNORECASE)
                if not uid:
                    raise PauseRun("登录后无法读取账号UID。")
                if r.account["game_uid"] and r.account["game_uid"] != uid[1]:
                    raise PauseRun("当前账号UID与保存记录不符。")
                r.account["game_uid"] = uid[1]
                r.ctx["uid_verified"] = True
                r.ctx.pop('title_auth_recovery', None)
                r.save()
                r.action("close-user-center", args=(612, 258))
                return
        if o.scene in ("title_menu", "home_menu") and r.ctx.get("uid_verified"):
            r.click("关闭", exact=True)
            return
        if o.scene == "title":
            r.click("TAP TO START")
        elif o.scene == "startup_reward":
            self.dismiss_reward()
        elif o.scene == "animation":
            r.click("SKIP", label="skip-opening")
        elif o.scene == "notice":
            r.click("关闭", exact=True)
        elif o.scene == "home":
            if r.ctx.get("gifts_done"):
                r.phase("draws")
                r.click("招募", exact=True)
            else:
                r.phase("gifts")
                r.action("open-gifts", args=(1542, 190))
        elif o.scene == "gift_box":
            if o.find("关闭（按下）", exact=True):
                if not r.ctx.get('gift_close_release_attempted'):
                    r.ctx['gift_close_release_attempted'] = True
                    r.save()
                    r.action('release-gift-close-touch', kind='release_touch')
                return
            if o.has("暂无可领取"):
                r.ctx["gifts_done"] = True
                r.save()
                r.click("关闭", exact=True)
            else:
                r.click("一键领取")
        elif o.scene in ("gift_confirm", "gift_received"):
            r.click("OK", exact=True, label="claim-gifts" if o.scene == "gift_confirm" else "ack-gifts")
