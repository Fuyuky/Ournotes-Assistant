"""Candidate selection is separate from transaction/business state."""

PHASE_PAGES = {
    "switch_account": ("login_history_confirm", "login_history_expanded", "login_history", "login_options",
                       "email_form", "user_center", "title_menu_unavailable", "title_menu", "home_menu", "return_title_confirm",
                       "notice", "startup_reward", "title", "home", "pool"),
    "login": ("email_form", "login_options", "login_history", "title"),
    "rewards": ("user_center", "title_menu_unavailable", "title_menu", "home_menu", "startup_reward", "notice", "home", "title", "animation"),
    "gifts": ("gift_confirm", "gift_received", "gift_box", "home", "startup_reward"),
    "draws": ("notice", "draw_confirm", "draw_result", "animation", "pool", "home", "title"),
    "logout": ("home_menu", "return_title_confirm", "title_menu_unavailable", "title_menu", "user_center", "login_history",
               "login_options", "email_form", "notice", "startup_reward", "title", "pool", "home"),
}
INTERRUPTS = ("server_maintenance", "required_update", "enable_prompt", "paid_shortage", "network_error", "review", "download_confirm", "downloading",
              "story_skip_confirm", "story_menu", "story", "opening_skip_confirm", "opening_movie", "character_dialogue", "loading")

# Allow expected transitions ahead of current-page revalidation. A background
# page must never win merely because the previous observation recognized it.
DESTINATIONS = {
    "pool-menu": ("home_menu",), "home-menu": ("home_menu",),
    "title-menu": ("title_menu_unavailable", "title_menu"),
    "verify-account-menu": ("title_menu_unavailable", "title_menu"),
    "close-unavailable-menu": ("title",),
    "关闭额外弹窗": ("login_history", "login_options", "email_form", "title"),
    "用户中心": ("user_center",), "open-gifts": ("gift_box",),
    "一键领取": ("gift_confirm",), "claim-gifts": ("gift_received",),
    "ack-gifts": ("gift_box",), "open-draw-confirmation": ("draw_confirm",),
    "招募TOP": ("pool",), "SKIP": ("draw_result",), "skip": ("draw_result",),
    "发送验证码": ("email_form",), "登录/注册": ("title",),
    "confirm-download": ("downloading", "loading", "notice", "startup_reward", "home"),
    "skip-opening-movie": ("opening_skip_confirm",),
    "confirm-opening-skip": ("opening_movie", "loading", "startup_reward", "notice", "home"),
    "open-story-menu": ("story_menu",), "skip-story": ("story_skip_confirm",),
    "confirm-story-skip": ("loading", "startup_reward", "notice", "home"),
    "标题画面": ("return_title_confirm", "title"), "退出登录": ("login_history", "login_options"),
}


def candidates(phase, last_action=None):
    requested = DESTINATIONS.get(last_action, ()) + PHASE_PAGES.get(phase, ())
    return tuple(dict.fromkeys(INTERRUPTS + requested))
