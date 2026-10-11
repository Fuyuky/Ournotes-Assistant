"""Stable account namespace. Public indices are permanent Gmail dot masks."""
# Synthetic default for fixtures. Production uses the server's private mailbox configuration.
MAILBOX = "ournotesexamplemailboxfortests@gmail.com"
TEST_START = 8
FORMAL_START = 1 << 20
MAX_EMAIL_LOCAL_LENGTH = 40


def mailbox_identity(address: str) -> str:
    local, domain = address.strip().lower().rsplit("@", 1)
    if domain in ("gmail.com", "googlemail.com"):
        return local.replace(".", "") + "@gmail.com"
    return local + "@" + domain


def alias_at(index: int, mailbox: str = MAILBOX) -> str:
    local, domain = mailbox.split("@")
    if type(index) is not int or not 0 <= index < 1 << (len(local) - 1):
        raise ValueError("别名区段已耗尽。")
    return "".join(c + ("." if i < len(local) - 1 and index & (1 << i) else "")
                   for i, c in enumerate(local)) + "@" + domain


def alias_is_usable(address: str) -> bool:
    """The game's 40-character limit applies before @, including inserted dots."""
    local, separator, domain = address.partition("@")
    return bool(local and separator and domain) and len(local) <= MAX_EMAIL_LOCAL_LENGTH


def next_usable_index(index: int, mailbox: str = MAILBOX) -> int:
    """Find the first usable mask without changing any historical alias mapping."""
    alias_at(index, mailbox)  # Validate the namespace and starting mask.
    local = mailbox.split("@", 1)[0]
    dot_budget = MAX_EMAIL_LOCAL_LENGTH - len(local)
    end = 1 << (len(local) - 1)
    if dot_budget < 0:
        raise ValueError("当前邮箱别名区段已耗尽。")
    while index < end:
        if index.bit_count() <= dot_budget:
            return index
        # Every intervening mask has these same set bits plus lower ones.
        # Carry the lowest set bit to skip the entire unusable interval.
        index += index & -index
    raise ValueError("当前邮箱别名区段已耗尽。")
