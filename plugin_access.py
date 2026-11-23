"""Workspace credentials, browser sessions and OAuth access to the archive."""

import hashlib
import hmac
import json
import os
import re
import secrets
import sqlite3
import time
from contextlib import contextmanager
from urllib.parse import urlsplit

from mcp.server.auth.provider import (
    AccessToken, AuthorizationCode, AuthorizationParams, AuthorizeError, RefreshToken,
    RegistrationError, TokenError, construct_redirect_uri,
)
from mcp.shared.auth import OAuthClientInformationFull, OAuthToken
from starlette.concurrency import run_in_threadpool
from starlette.responses import HTMLResponse, RedirectResponse, Response

from admin_access import ADMIN_LOGIN

SCOPE = "archive:read"
ADMIN_PLUGIN_PASSWORD = "admin_plugin_password"
LOCAL_ORIGIN = "http://127.0.0.1:8079"
USER_COOKIE = "wa_archive_user"
USER_SESSION_SECONDS = 8 * 3600
NO_STORE = {
    "Cache-Control": "no-store",
    "Referrer-Policy": "no-referrer",
    "X-Content-Type-Options": "nosniff",
    "Content-Security-Policy": "default-src 'none'; style-src 'self'; font-src 'self'; "
                               "form-action 'self' https://chatgpt.com; frame-ancestors 'none'; base-uri 'none'",
}


def mcp_origin():
    origin = os.environ.get("WA_HISTORY_MCP_ORIGIN", LOCAL_ORIGIN).rstrip("/")
    url = urlsplit(origin)
    if (origin != LOCAL_ORIGIN and url.scheme != "https") or not url.hostname or (
        url.path or url.query or url.fragment or url.username or url.password
    ):
        raise ValueError("WA_HISTORY_MCP_ORIGIN должен быть HTTPS-адресом без пути.")
    return origin


def secret_key(value):
    return hashlib.sha256(value.encode()).hexdigest()


def password_hash(value, salt):
    return hashlib.scrypt(value.encode(), salt=salt, n=2**15, r=8, p=1, maxmem=64 * 1024**2).hex()


def credentials(value):
    if not isinstance(value, str) or not 12 <= len(value) <= 128 or not value.strip():
        raise ValueError("Пароль должен содержать от 12 до 128 символов.")
    salt = secrets.token_bytes(16)
    return {"salt": salt.hex(), "hash": password_hash(value, salt)}


def verifies(settings, password):
    salt = bytes.fromhex(settings["salt"]) if settings else bytes(16)
    digest = password_hash(password, salt)
    return bool(settings and hmac.compare_digest(digest, settings["hash"]))


def user_name(value):
    return value.strip().lower() if isinstance(value, str) else ""


class AccessStore:
    def __init__(self, state):
        self.path = state / "plugin-access.sqlite3"

    @contextmanager
    def db(self, *, write=False):
        self.path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        self.path.touch(mode=0o600, exist_ok=True)
        connection = sqlite3.connect(self.path, timeout=10)
        try:
            with connection:
                connection.execute("""CREATE TABLE IF NOT EXISTS access (
                    kind TEXT, key TEXT, payload TEXT, expires INTEGER,
                    PRIMARY KEY(kind, key))""")
                if write:
                    connection.execute("BEGIN IMMEDIATE")
                    connection.execute("DELETE FROM access WHERE expires > 0 AND expires <= ?", (int(time.time()),))
                yield connection
        finally:
            connection.close()

    def read(self, db, kind, key):
        row = db.execute("""SELECT payload FROM access WHERE kind=? AND key=?
                            AND (expires=0 OR expires>?)""", (kind, key, int(time.time()))).fetchone()
        return json.loads(row[0]) if row else None

    def save(self, db, kind, key, payload, expires=0):
        db.execute("INSERT OR REPLACE INTO access VALUES (?,?,?,?)",
                   (kind, key, json.dumps(payload), expires))

    def remove(self, db, kind, key):
        return db.execute("DELETE FROM access WHERE kind=? AND key=?", (kind, key)).rowcount

    def managed(self, db):
        return bool(db.execute("SELECT 1 FROM access WHERE kind='user' LIMIT 1").fetchone())

    def active_user(self, db, username):
        user = self.read(db, "user", username)
        return bool(user and user["enabled"])

    def plugin_credentials(self, db, username):
        if username == ADMIN_LOGIN:
            return self.read(db, "config", ADMIN_PLUGIN_PASSWORD)
        user = self.read(db, "user", username)
        return user if user and user["enabled"] else None

    def users(self):
        if not self.path.exists():
            return []
        with self.db() as db:
            return [{"username": row[0], **{key: user[key] for key in ("name", "enabled", "created_at")}}
                    for row in db.execute("SELECT key,payload FROM access WHERE kind='user' ORDER BY key")
                    if (user := json.loads(row[1]))]

    def change_user(self, action, username, *, name=None, password=None):
        username = user_name(username)
        if not re.fullmatch(r"[a-z0-9][a-z0-9._-]{2,31}", username):
            raise ValueError("Логин — от 3 до 32 латинских букв, цифр, точек, дефисов или подчёркиваний.")
        if action == "create" and username == ADMIN_LOGIN:
            raise ValueError("Выберите другой логин. Этот используется администратором.")
        name = name.strip() if isinstance(name, str) else ""
        if action == "create" and not 1 <= len(name) <= 100:
            raise ValueError("Введите имя пользователя до 100 символов.")
        secret = credentials(password) if action in ("create", "password") else None
        with self.db(write=True) as db:
            user = self.read(db, "user", username)
            if action == "create":
                if user:
                    raise ValueError("Пользователь с таким логином уже есть.")
                user = {"name": name, "enabled": True, "created_at": int(time.time()), **secret}
            elif not user:
                raise ValueError("Пользователь не найден.")
            elif action == "password":
                user.update(secret)
            else:
                user["enabled"] = action == "enable"
            if action in ("disable", "password"):
                db.execute("""DELETE FROM access
                              WHERE kind IN ('web_session','code','access','refresh')
                              AND json_extract(payload,'$.subject')=?""", (username,))
            self.save(db, "user", username, user)

    def web_access(self, token):
        if not self.path.exists():
            return False, None
        with self.db() as db:
            if not self.managed(db):
                return False, None
            session = self.read(db, "web_session", secret_key(token)) if token else None
            username = session["subject"] if session else None
            return True, username if username and username != ADMIN_LOGIN and self.active_user(db, username) else None

    def check_login(self, db, bucket, settings, password):
        failures = self.read(db, "attempts", bucket) or 0
        if failures >= 5:
            return 429
        if not verifies(settings, password):
            self.save(db, "attempts", bucket, failures + 1, int(time.time()) + 60)
            return 401
        self.remove(db, "attempts", bucket)
        return 200

    def web_login(self, username, password):
        username = user_name(username)
        if username == ADMIN_LOGIN:
            return None, 401
        if not isinstance(password, str) or not 1 <= len(password) <= 128:
            return None, 401
        with self.db(write=True) as db:
            user = self.read(db, "user", username)
            status = self.check_login(db, "web", user if user and user["enabled"] else None, password)
            if status != 200:
                return None, status
            token = secrets.token_urlsafe(32)
            self.save(db, "web_session", secret_key(token), {"subject": username},
                      int(time.time()) + USER_SESSION_SECONDS)
            return token, 200

    def web_logout(self, token):
        if token and self.path.exists():
            with self.db(write=True) as db:
                self.remove(db, "web_session", secret_key(token))

    def status(self, subject):
        member = subject != ADMIN_LOGIN
        if not self.path.exists():
            return {"password_set": False, "sessions": 0, "named_users": member}
        with self.db() as db:
            sessions = db.execute("""SELECT COUNT(*) FROM access WHERE kind='refresh' AND expires>?
                                      AND json_extract(payload,'$.subject')=?""",
                                  (int(time.time()), subject)).fetchone()[0]
            return {"password_set": not member and bool(self.plugin_credentials(db, subject)),
                    "named_users": member, "sessions": sessions}

    def set_admin_plugin_password(self, value):
        secret = credentials(value)
        with self.db(write=True) as db:
            self.save(db, "config", ADMIN_PLUGIN_PASSWORD, secret)
            self.revoke(db, ADMIN_LOGIN)

    def revoke(self, db, subject):
        db.execute("""DELETE FROM access WHERE kind IN ('code','access','refresh')
                      AND json_extract(payload,'$.subject')=?""", (subject,))


class OwnerOAuth:
    def __init__(self, store, origin):
        self.store = store
        self.origin = origin
        self.resource = origin + "/mcp"

    async def get_client(self, client_id):
        with self.store.db() as db:
            value = self.store.read(db, "client", client_id)
        return OAuthClientInformationFull.model_validate(value) if value else None

    async def register_client(self, client):
        redirects = client.redirect_uris or []
        if not redirects or any(not self.chatgpt_redirect(uri) for uri in redirects):
            raise RegistrationError("invalid_redirect_uri", "Use the OAuth callback issued by ChatGPT.")
        with self.store.db(write=True) as db:
            self.store.save(db, "client", client.client_id, client.model_dump(mode="json"))

    def chatgpt_redirect(self, uri):
        url = urlsplit(str(uri))
        return (url.scheme == "https" and url.netloc == "chatgpt.com"
                and not (url.query or url.fragment)
                and (url.path == "/connector_platform_oauth_redirect"
                     or re.fullmatch(r"/connector/oauth/[A-Za-z0-9_-]+", url.path)))

    async def authorize(self, client, params: AuthorizationParams):
        if params.resource and params.resource != self.resource:
            raise AuthorizeError("invalid_request", "Invalid resource")
        if not re.fullmatch(r"[A-Za-z0-9_-]{43}", params.code_challenge):
            raise AuthorizeError("invalid_request", "Invalid PKCE challenge")
        with self.store.db(write=True) as db:
            if not self.store.managed(db) and not self.store.plugin_credentials(db, ADMIN_LOGIN):
                raise AuthorizeError("access_denied", "Configure the access password in the archive admin first.")
            nonce = secrets.token_urlsafe(32)
            self.store.save(db, "login", secret_key(nonce),
                            {"client": client.client_id, "params": params.model_dump(mode="json")},
                            int(time.time()) + 600)
        return self.origin + "/owner-login?request=" + nonce

    def check_password(self, nonce, password, username=""):
        if not re.fullmatch(r"[A-Za-z0-9_-]{43}", nonce) or not 1 <= len(password) <= 128:
            return None, 400
        with self.store.db(write=True) as db:
            login = self.store.read(db, "login", secret_key(nonce))
            if not login:
                return None, 410
            subject = user_name(username)
            settings = self.store.plugin_credentials(db, subject)
            status = self.store.check_login(db, "owner", settings, password)
            if status != 200:
                return None, status
            self.store.remove(db, "login", secret_key(nonce))
            params = AuthorizationParams.model_validate(login["params"])
            code = secrets.token_urlsafe(32)
            expires = int(time.time()) + 90
            grant = AuthorizationCode(code="", scopes=[SCOPE], expires_at=expires,
                                      client_id=login["client"], code_challenge=params.code_challenge,
                                      redirect_uri=params.redirect_uri, resource=self.resource, subject=subject,
                                      redirect_uri_provided_explicitly=params.redirect_uri_provided_explicitly)
            self.store.save(db, "code", secret_key(code), grant.model_dump(mode="json"), expires)
            return construct_redirect_uri(str(params.redirect_uri), code=code, state=params.state), 303

    async def login_page(self, request):
        nonce = request.query_params.get("request", "")
        error, status = "", 200
        if request.method == "POST":
            if request.headers.get("origin") != self.origin:
                return Response(status_code=403, headers=NO_STORE)
            length = request.headers.get("content-length", "")
            if not length.isdigit() or int(length) > 4096:
                return Response(status_code=413, headers=NO_STORE)
            async with request.form() as form:
                nonce, password, username = form.get("request", ""), form.get("password", ""), form.get("username", "")
                if not all(isinstance(value, str) for value in (nonce, password, username)):
                    return Response(status_code=400, headers=NO_STORE)
                redirect, status = await run_in_threadpool(self.check_password, nonce, password, username)
            if redirect:
                return RedirectResponse(redirect, status_code=303, headers=NO_STORE)
            error = {400: "Проверьте данные входа.", 401: "Логин или пароль не подошёл.",
                     410: "Запрос истёк. Откройте подключение в ChatGPT снова.",
                     429: "Слишком много попыток. Подождите минуту."}[status]
        if not re.fullmatch(r"[A-Za-z0-9_-]{43}", nonce) or status == 410:
            return HTMLResponse("Откройте подключение заново в ChatGPT.", status_code=410, headers=NO_STORE)
        return HTMLResponse(
            '<!doctype html><html lang="ru"><meta charset="utf-8">'
            '<meta name="viewport" content="width=device-width,initial-scale=1">'
            '<title>Доступ к архиву WhatsApp</title><link rel="stylesheet" href="/assets/archive.css">'
            '<main class="oauth-shell"><section class="panel"><header class="page-heading">'
            '<h1>Доступ к архиву WhatsApp</h1>'
            '<p>Войдите, чтобы разрешить ChatGPT поиск по переписке.</p></header>'
            f'<p role="alert" class="notice error"{"" if error else " hidden"}>{error}</p><form method="post">'
            f'<input type="hidden" name="request" value="{nonce}">'
            '<label class="field">Логин <input name="username" required maxlength="32" '
            'autocomplete="username"></label>'
            '<label class="field">Пароль <input name="password" type="password" required maxlength="128" '
            'autocomplete="current-password"></label><div class="actions"><button>Разрешить поиск</button>'
            '</div></form></section></main></html>',
            # A form POST must retain its Origin. Keep the login URL out of cross-origin referrers.
            status_code=status, headers={**NO_STORE, "Referrer-Policy": "same-origin"},
        )

    def load_grant(self, kind, value, cls, field):
        with self.store.db() as db:
            record = self.store.read(db, kind, secret_key(value))
            if record and not self.store.plugin_credentials(db, record.get("subject")):
                return None
        return cls.model_validate({**record, field: value}) if record else None

    async def load_authorization_code(self, client, authorization_code):
        return self.load_grant("code", authorization_code, AuthorizationCode, "code")

    async def load_refresh_token(self, client, refresh_token):
        return self.load_grant("refresh", refresh_token, RefreshToken, "token")

    async def load_access_token(self, token):
        return self.load_grant("access", token, AccessToken, "token")

    def exchange(self, kind, value, client_id):
        with self.store.db(write=True) as db:
            key = secret_key(value)
            grant = self.store.read(db, kind, key)
            if not grant or grant["client_id"] != client_id:
                raise TokenError("invalid_grant", "Grant expired or already used")
            subject = grant.get("subject")
            if not self.store.plugin_credentials(db, subject):
                raise TokenError("invalid_grant", "Workspace access was revoked")
            self.store.remove(db, kind, key)
            now = int(time.time())
            access, refresh = secrets.token_urlsafe(32), secrets.token_urlsafe(32)
            for label, token, ttl in (("access", access, 3600), ("refresh", refresh, 30 * 86400)):
                expires = now + ttl
                self.store.save(db, label, secret_key(token), {"token": "", "client_id": client_id,
                                "scopes": [SCOPE], "expires_at": expires, "resource": self.resource,
                                "subject": subject}, expires)
            return OAuthToken(access_token=access, refresh_token=refresh, expires_in=3600, scope=SCOPE)

    async def exchange_authorization_code(self, client, authorization_code):
        return self.exchange("code", authorization_code.code, client.client_id)

    async def exchange_refresh_token(self, client, refresh_token, scopes):
        return self.exchange("refresh", refresh_token.token, client.client_id)

    async def revoke_token(self, token):
        with self.store.db(write=True) as db:
            self.store.remove(db, "access" if isinstance(token, AccessToken) else "refresh", secret_key(token.token))
