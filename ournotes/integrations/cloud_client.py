"""Authenticated cloud transport; no Gmail credential or local allocation fallback."""
from __future__ import annotations

import hashlib
import json
import uuid
from dataclasses import dataclass
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlencode, urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener

from ournotes.common.secrets import SecretStore
from ournotes.paths import DATA_ROOT


class CloudError(RuntimeError):
    def __init__(self, code, message, status=None):
        super().__init__(message)
        self.code, self.status = code, status


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def response_error(status, value):
    if status == 424 and isinstance(value, dict) and value.get('error') == 'gmail_authorization_required':
        return CloudError('gmail_authorization_required',
                          '云端 Gmail 授权已失效，请联系 Fuyuki 使用独立授权工具重新授权。', status)
    return CloudError('http_error', f'云端请求失败（HTTP {status}）。', status)


def validate_service_url(value):
    parsed = urlsplit(value)
    loopback = parsed.hostname in {"127.0.0.1", "localhost", "::1"}
    if (parsed.scheme != "https" and not (parsed.scheme == "http" and loopback)) or not parsed.hostname:
        raise ValueError("服务地址必须使用 HTTPS；仅本机测试允许 HTTP。")
    if parsed.username or parsed.password or parsed.query or parsed.fragment or parsed.path not in ("", "/"):
        raise ValueError("服务地址只包含协议、主机和端口。")
    return value.rstrip("/")


@dataclass(frozen=True)
class CloudClientConfig:
    service_url: str
    credential: str

    @classmethod
    def _store(cls, root):
        identity = hashlib.sha256(str(Path(root).resolve()).casefold().encode()).hexdigest()[:24]
        return SecretStore(), "cloud-" + identity

    @classmethod
    def load(cls, root=DATA_ROOT):
        store, name = cls._store(root)
        data = store.load(name)
        if data is None:
            return None
        return cls(validate_service_url(data["service_url"]), data["credential"])

    @classmethod
    def save(cls, root, service_url, credential):
        url = validate_service_url(service_url)
        if not isinstance(credential, str) or len(credential) < 16 or any(c.isspace() for c in credential):
            raise ValueError("请填写有效的独立接入凭据。")
        store, name = cls._store(root)
        store.save(name, {"service_url": url, "credential": credential})
        return {"service_url": url, "configured": True}

    def public(self):
        return {"service_url": self.service_url, "configured": True}


class CloudClient:
    PREFIX = "/api/cloud/v1"

    def __init__(self, service_url, credential, *, transport=None, timeout=20):
        self.service_url = validate_service_url(service_url)
        self.credential, self.timeout, self.transport = credential, timeout, transport

    @classmethod
    def from_root(cls, root=DATA_ROOT):
        config = CloudClientConfig.load(root)
        if config is None:
            raise CloudError("not_configured", "请先在控制台配置云端连接。")
        return cls(config.service_url, config.credential)

    def request(self, method, path, body=None, *, timeout=None):
        headers = {"Authorization": "Bearer " + self.credential, "Accept": "application/json"}
        if self.transport is not None:
            response = self.transport.request(method, self.PREFIX + path, json=body, headers=headers,
                                              follow_redirects=False)
            if response.status_code >= 300:
                try:
                    value = response.json()
                except ValueError:
                    value = None
                raise response_error(response.status_code, value)
            return response.json()
        raw = None if body is None else json.dumps(body, ensure_ascii=False).encode()
        if raw is not None:
            headers["Content-Type"] = "application/json"
        request = Request(self.service_url + self.PREFIX + path, data=raw, headers=headers, method=method)
        try:
            with build_opener(NoRedirect()).open(
                    request, timeout=self.timeout if timeout is None else min(self.timeout, timeout)) as response:
                return json.load(response)
        except HTTPError as error:
            try:
                value = json.loads(error.read(4096))
            except (ValueError, OSError):
                value = None
            raise response_error(error.code, value) from None
        except (URLError, TimeoutError, OSError):
            raise CloudError("unavailable", "云端暂时不可达，已保留本地请求与成果。") from None

    def health(self):
        return self.request("GET", "/health")

    def allocate(self, request_id, account_id, channel):
        return self.request("POST", "/allocations", {"request_id": request_id, "account_id": account_id, "channel": channel})

    def list_results(self):
        return self.request("GET", "/results")

    def list_accounts(self, *, index=None, owner_username=None):
        query = {key: value for key, value in
                 (("index", index), ("owner_username", owner_username)) if value is not None}
        return self.request("GET", "/accounts" + ("?" + urlencode(query) if query else ""))

    def presets(self):
        return self.request("GET", "/presets")

    def card_art(self, kind, card_id):
        if kind not in {'member', 'support'} or not str(card_id).isdigit():
            raise ValueError('无效的卡牌身份。')
        path = self.PREFIX + f'/art/{kind}/{int(card_id)}'
        headers = {'Authorization': 'Bearer ' + self.credential}
        maximum = 5 * 1024 * 1024
        if self.transport is not None:
            response = self.transport.request('GET', path, headers=headers, follow_redirects=False)
            if response.status_code != 200:
                raise response_error(response.status_code, None)
            content, media = response.content, response.headers.get('content-type', '')
        else:
            try:
                with build_opener(NoRedirect()).open(
                        Request(self.service_url + path, headers=headers), timeout=self.timeout) as response:
                    content = response.read(maximum + 1)
                    media = response.headers.get('Content-Type', '')
            except HTTPError as error:
                raise response_error(error.code, None) from None
            except (URLError, TimeoutError, OSError):
                raise CloudError('unavailable', '卡图暂时不可用。') from None
        media = media.split(';', 1)[0].strip().lower()
        if len(content) > maximum or media not in {'image/png', 'image/jpeg', 'image/webp'}:
            raise CloudError('invalid_art', '云端卡图格式或大小不符合要求。')
        return content, media

    def save_preset(self, key, preset, expected_version):
        return self.request("PUT", "/presets/" + quote(key, safe=""),
                            {"expected_version": expected_version, "preset": preset})

    def delete_preset(self, key, expected_version):
        return self.request("DELETE", "/presets/" + quote(key, safe="")
                            + "?" + urlencode({"expected_version": expected_version}))

    def sync_manifest(self, *, since=None, inventory=False):
        query = f"?inventory={'true' if inventory else 'false'}"
        if since is not None:
            query += f"&since={int(since)}"
        return self.request("GET", "/sync" + query)

    def result_subset(self, account_ids):
        return self.request("POST", "/sync/results", {"account_ids": account_ids})["results"]

    def get_result(self, account_id):
        return self.request("GET", "/results/" + account_id)

    def get_result_by_index(self, index):
        if type(index) is not int or not 0 <= index <= 2**53 - 1:
            raise ValueError("请填写有效账号编号。")
        return self.request("GET", f"/results/by-index/{index}")

    def upload(self, result, event_id, version=1):
        return self.request("POST", "/results",
                            {"event_id": event_id, "version": version, "result": result})

    def set_used(self, account_id, used, expected_version, request_id=None):
        return self.request("POST", f"/results/{account_id}/used",
                            {"used": used, "expected_version": expected_version, "request_id": request_id or uuid.uuid4().hex})

    def usage_capability(self):
        return self.request('GET', '/usage/capability')

    def set_usage(self, account_id, target_status, expected_version, request_id=None):
        return self.request('POST', f'/results/{account_id}/usage',
                            {'target_status': target_status, 'expected_version': expected_version,
                             'request_id': request_id or uuid.uuid4().hex})

    def usage_receipt(self, account_id, request_id):
        return self.request('GET', f'/results/{account_id}/usage/requests/{request_id}')




    def prepare_mail(self, account_id, alias, request_id, timeout=120, *, request_timeout=None):
        return self.request("POST", "/mail/tickets", {"account_id": account_id, "alias": alias,
                            "request_id": request_id, "timeout": timeout}, timeout=request_timeout)

    def mail_sent(self, ticket, *, request_timeout=None):
        return self.request("POST", f"/mail/tickets/{ticket}/sent", {}, timeout=request_timeout)

    def retry_mail(self, ticket, request_id, reason, timeout=120, *, request_timeout=None):
        return self.request('POST', f'/mail/tickets/{ticket}/retry',
                            {'request_id': request_id, 'reason': reason, 'timeout': timeout}, timeout=request_timeout)

    def poll_mail(self, ticket, *, request_timeout=None):
        return self.request("GET", f"/mail/tickets/{ticket}", timeout=request_timeout)

    def finish_mail(self, ticket):
        return self.request("POST", f"/mail/tickets/{ticket}/complete", {})
