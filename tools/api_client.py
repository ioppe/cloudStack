"""Small same-origin API client for administrator scripts. Never logs credentials."""
import http.cookiejar
import json
from pathlib import Path
import urllib.error
import urllib.request


class Client:
    def __init__(self, base, credentials):
        self.base = base.rstrip("/")
        self.csrf = ""
        self.jar = http.cookiejar.CookieJar()
        self.opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), urllib.request.HTTPCookieProcessor(self.jar))
        lines = Path(credentials).read_text(encoding="utf-8").splitlines()
        self.password = next(x.split("：", 1)[1] for x in lines if x.startswith("密码："))

    def call(self, method, path, data=None, expected=200):
        raw = json.dumps(data or {}, ensure_ascii=False).encode() if method != "GET" else None
        req = urllib.request.Request(self.base + path, data=raw, method=method, headers={"Content-Type": "application/json", "X-CSRF-Token": self.csrf})
        try:
            response = self.opener.open(req, timeout=120)
        except urllib.error.HTTPError as exc:
            response = exc
        body = json.loads(response.read())
        if response.code != expected:
            raise AssertionError("{} {}: expected {}, got {}: {}".format(method, path, expected, response.code, body.get("error", "")))
        return body

    def login(self):
        data = self.call("POST", "/api/login", {"username": "admin", "password": self.password})
        self.csrf = data["csrf_token"]
        return data
