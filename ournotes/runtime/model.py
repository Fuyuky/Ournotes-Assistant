"""Runtime signals and login field contract."""
class PauseRun(RuntimeError):
    pass


MAIL_LIMIT_MESSAGE = '今日邮箱发送次数已达上限'
LOGIN_FORM_FIELDS = ('email_focus', 'email_filled', 'agreement', 'code_focus', 'code_filled')
