"""Access to the administration screens of this local workspace."""

import hashlib
import hmac
import os
import secrets
import tempfile
import time

COOKIE = "wa_archive_admin"
ADMIN_LOGIN = "admin"
SESSION_SECONDS = 8 * 3600


def initialize_admin(state):
    state.mkdir(mode=0o700, parents=True, exist_ok=True)
    for key in (state / "admin.key", state / "admin-session.key"):
        if key.exists():
            continue
        with tempfile.NamedTemporaryFile(dir=state) as temporary:
            temporary.write(secrets.token_urlsafe(32).encode("ascii"))
            temporary.flush()
            try:
                os.link(temporary.name, key)
            except FileExistsError:
                pass


def key_bytes(state):
    return (state / "admin.key").read_bytes().strip()


def accepts_key(state, value):
    return hmac.compare_digest(key_bytes(state), value.encode())


def issue_session(state):
    expires = str(int(time.time()) + SESSION_SECONDS)
    signature = hmac.new((state / "admin-session.key").read_bytes(), expires.encode(), hashlib.sha256).hexdigest()
    return expires + "." + signature


def is_admin(request, state):
    value = request.cookies.get(COOKIE, "")
    expires, _, signature = value.partition(".")
    if not (expires.isascii() and expires.isdecimal() and len(expires) <= 12 and len(signature) == 64):
        return False
    if int(expires) <= time.time():
        return False
    try:
        expected = hmac.new((state / "admin-session.key").read_bytes(), expires.encode(), hashlib.sha256).hexdigest()
    except FileNotFoundError:
        return False
    return hmac.compare_digest(expected.encode(), signature.encode())
