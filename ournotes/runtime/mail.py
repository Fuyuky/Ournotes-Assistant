"""Client-side OTP adapter. All mailbox access belongs to the cloud service."""
import random
import time

from ournotes.common.otp import NeedsReview
from ournotes.integrations.cloud_client import CloudClient, CloudError
from ournotes.integrations.cloud_sync import CloudSync
from ournotes.paths import DATA_ROOT

MAIL_REQUEST_ATTEMPTS = 6
MAIL_REQUEST_BUDGET_SECONDS = 90
MAIL_REQUEST_BACKOFF_SECONDS = (2, 4, 8, 16, 30)


class RunnerMail:
    def __init__(self, root=DATA_ROOT, client=None, *, observer=None):
        self.root, self.client = root, client
        self.observer = observer or (lambda **_: None)

    def connected(self):
        if self.client is None:
            self.client = CloudClient.from_root(self.root)
        return self.client

    def request(self, operation, *args, expires_at=None):
        # These calls reuse a persisted request/ticket and never send game mail.
        # Retry transient transport failures in the worker, with a finite budget.
        started = time.monotonic()
        deadline = started + MAIL_REQUEST_BUDGET_SECONDS
        last_error = None
        for attempt in range(MAIL_REQUEST_ATTEMPTS):
            remaining = deadline - time.monotonic()
            if expires_at is not None:
                remaining = min(remaining, expires_at - time.time())
            if remaining <= 0:
                raise last_error or CloudError('deadline_exceeded', '云端收码等待期限已到，已保留原请求与票据。')
            try:
                value = getattr(self.connected(), operation)(*args, request_timeout=remaining)
                if attempt:
                    self.observer(operation=operation, attempt=attempt + 1, status='recovered',
                                  seconds=round(time.monotonic() - started, 3))
                return value
            except CloudError as error:
                last_error = error
                transient = error.code == 'unavailable' or error.status in (408, 429, 500, 502, 503, 504)
                remaining = deadline - time.monotonic()
                if expires_at is not None:
                    remaining = min(remaining, expires_at - time.time())
                base = MAIL_REQUEST_BACKOFF_SECONDS[min(attempt, len(MAIL_REQUEST_BACKOFF_SECONDS) - 1)]
                delay = random.uniform(base, base * 1.2)
                retry = transient and attempt + 1 < MAIL_REQUEST_ATTEMPTS and delay < remaining
                self.observer(operation=operation, attempt=attempt + 1, status='retry' if retry else 'failed',
                              error=error.code, http_status=error.status, delay=round(delay, 3) if retry else 0,
                              seconds=round(time.monotonic() - started, 3))
                if not retry:
                    raise
                time.sleep(delay)

    def prepare(self, alias, account_id, timeout=120):
        # Lost responses and resumes cannot create a second send attempt.
        value = self.request('prepare_mail', account_id, alias, 'otp-' + account_id, timeout)
        return {'ticket': value['ticket_id'], 'status': value['status'], 'expires_at': value['expires_at']}

    def mark_sent(self, ticket):
        return self.request('mail_sent', ticket)

    def retry(self, ticket, account_id, reason, timeout=120):
        request_id = 'otp-limit-' + ticket if reason == 'proxy_limit' else 'otp-retry-' + account_id
        value = self.request('retry_mail', ticket, request_id, reason, timeout)
        return {'ticket': value['ticket_id'], 'status': value['status'], 'expires_at': value['expires_at']}

    def poll(self, ticket, expires_at):
        value = self.request('poll_mail', ticket, expires_at=expires_at)
        status = value['status']
        if status == 'ambiguous':
            raise NeedsReview('验证码存在多个候选，需要核查；不会自动重新发码。')
        if status == 'ready':
            return {'status': 'received', 'code': value['code']}
        return {'status': status}

    def finish(self, ticket):
        sync = CloudSync(self.root)
        sync.finish_mail(ticket)
        return sync.try_flush(self.client)

    def close(self):
        pass
