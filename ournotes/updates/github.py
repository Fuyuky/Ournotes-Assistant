"""Anonymous HTTPS reads from the public project's stable GitHub releases."""
from __future__ import annotations

import json
import re
import urllib.error
import urllib.parse
import urllib.request

PUBLIC_REPOSITORY = "Fuyuky/Ournotes-Assistant"


class SafeRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, request, fp, code, msg, headers, newurl):
        parsed = urllib.parse.urlsplit(newurl)
        if parsed.scheme != "https" or parsed.hostname not in ("api.github.com", "github.com", "release-assets.githubusercontent.com", "objects.githubusercontent.com"):
            raise ValueError("GitHub重定向目标不在允许范围。")
        redirected = super().redirect_request(request, fp, code, msg, headers, newurl)
        if parsed.hostname != "api.github.com":
            redirected.remove_header("Authorization")
        return redirected


class GitHubReleases:
    def __init__(self, repository=PUBLIC_REPOSITORY, transport=None):
        if not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", repository):
            raise ValueError("GitHub仓库格式应为owner/repository。")
        if repository != PUBLIC_REPOSITORY:
            raise ValueError("客户端更新只读取公开项目仓库。")
        self.repository, self.transport = repository, transport
        self.opener = urllib.request.build_opener(SafeRedirect())

    def _request(self, path, accept="application/vnd.github+json"):
        url = "https://api.github.com" + path
        headers = {"Accept": accept, "X-GitHub-Api-Version": "2022-11-28", "User-Agent": "OurNotes-Updater"}
        if self.transport:
            return self.transport(url, headers)
        try:
            return self.opener.open(urllib.request.Request(url, headers=headers), timeout=30)
        except (urllib.error.URLError, TimeoutError, OSError) as error:
            raise ValueError("公开更新服务暂不可达；批次运行不受影响。") from error

    def latest(self):
        with self._request(f"/repos/{self.repository}/releases/latest") as response:
            release = json.load(response)
        if release.get("draft") or release.get("prerelease"):
            raise ValueError("客户端不接收草稿或预发布版本。")
        return release

    def asset(self, identifier):
        if type(identifier) is not int or identifier <= 0:
            raise ValueError("发布资产ID无效。")
        return self._request(f"/repos/{self.repository}/releases/assets/{identifier}", "application/octet-stream")

    def package(self, version, name):
        from ournotes.updates.manifest import version_tuple
        version_tuple(version)
        if name != f'ournotes-{version}-windows-x64.zip':
            raise ValueError('发行包名称无效。')
        url = f'https://github.com/{self.repository}/releases/download/v{version}/{name}'
        headers = {'User-Agent': 'OurNotes-Updater'}
        if self.transport:
            return self.transport(url, headers)
        return self.opener.open(urllib.request.Request(url, headers=headers), timeout=60)
