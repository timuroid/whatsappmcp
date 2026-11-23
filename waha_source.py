"""Local WAHA connector for discovery and selected-chat ingestion."""

import json
import os
import re
import secrets
import subprocess
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import quote, unquote, urlsplit

import httpx

from archive import database, digest, identifier, prepare_index, reindex, save_blob, write_message

BASE_URL = "http://127.0.0.1:3031"
SESSION = "/api/sessions/default"
AUTH = "/api/default/auth"
CHATS = "/api/default/chats"
SUFFIXES = ("@g.us", "@c.us", "@s.whatsapp.net", "@lid")
PAGE = 100
MAX_MEDIA = 512 * 1024 * 1024
OVERLAP = 2 * 86400
RESCAN = 7 * 86400


def compose_waha(state, key, *, personal=False):
    project = "whatsapp-history"
    if personal:
        project += "-" + digest(str(state.resolve()))[:16]
    args = ["docker", "compose", "-p", project, "-f", str(Path(__file__).with_name("compose.archive.yml"))]
    env = {**os.environ, "WA_HISTORY_WAHA_KEY": key,
           "WA_HISTORY_WAHA_SESSIONS": str(state / "waha-sessions"),
           "WA_HISTORY_WAHA_PORT": "" if personal else "3031"}
    return args, env


def initialize_waha(state, *, personal=False):
    state.mkdir(mode=0o700, parents=True, exist_ok=True)
    key_path = state / "waha.key"
    if not key_path.exists():
        with tempfile.NamedTemporaryFile(dir=state) as temporary:
            temporary.write(secrets.token_hex(32).encode())
            temporary.flush()
            try:
                os.link(temporary.name, key_path)
            except FileExistsError:
                pass
    sessions = state / "waha-sessions"
    sessions.mkdir(mode=0o700, exist_ok=True)
    args, env = compose_waha(state, key_path.read_text().strip(), personal=personal)
    subprocess.run(args + ["up", "-d", "waha"], env=env, stdout=subprocess.DEVNULL,
                   stderr=subprocess.DEVNULL, check=True, timeout=180)


def personal_waha_url(state, key):
    args, env = compose_waha(state, key, personal=True)
    try:
        result = subprocess.run(args + ["port", "waha", "3000"], env=env, capture_output=True,
                                text=True, check=True, timeout=5)
    except subprocess.SubprocessError as exc:
        raise OSError("Personal WhatsApp instance is not running") from exc
    address = result.stdout.strip()
    if not re.fullmatch(r"127\.0\.0\.1:[0-9]{1,5}", address):
        raise OSError("Personal WhatsApp instance has no loopback port")
    return "http://" + address


def jid(value):
    if isinstance(value, dict):
        value = value.get("_serialized") or value.get("id")
    return value if isinstance(value, str) else ""


def utc_at(seconds):
    return datetime.fromtimestamp(seconds, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


class Waha:
    def __init__(self, state, transport=None, *, personal=False):
        key = (state / "waha.key").read_text().strip()
        url = personal_waha_url(state, key) if personal else BASE_URL
        self.http = httpx.Client(base_url=url, headers={"X-Api-Key": key},
                                 timeout=30, trust_env=False, transport=transport)

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.http.close()

    def get(self, path, **params):
        return self.http.get(path, params=params).raise_for_status().json()

    def session(self):
        response = self.http.get(SESSION, timeout=5)
        return {} if response.status_code == 404 else response.raise_for_status().json()

    def connect(self, *, renew_qr=False):
        status = self.session().get("status")
        if not status:
            self.http.post("/api/sessions", json={"name": "default", "start": True}).raise_for_status()
        elif status == "STOPPED":
            self.http.post(SESSION + "/start", json={}).raise_for_status()
        elif status == "FAILED" or (status == "SCAN_QR_CODE" and renew_qr):
            self.http.post(SESSION + "/restart", json={}).raise_for_status()

    def reconnect(self):
        status = self.session().get("status")
        if status and status != "SCAN_QR_CODE":
            self.http.post(SESSION + "/logout", json={}).raise_for_status()
        self.connect()

    def account(self):
        response = self.session()
        if response.get("status") != "WORKING":
            raise ValueError("WhatsApp ещё не подключён. Проверьте связанное устройство на телефоне.")
        me = response.get("me") or self.get(SESSION + "/me")
        identity = jid(me.get("id"))
        if not identity:
            raise ValueError("WhatsApp пока не вернул данные аккаунта. Попробуйте снова.")
        return identity.replace("@s.whatsapp.net", "@c.us")

    def qr(self):
        if self.session().get("status") != "SCAN_QR_CODE":
            return None
        response = self.http.get(AUTH + "/qr", headers={"Accept": "image/png"}).raise_for_status()
        if response.content.startswith(b"\x89PNG\r\n\x1a\n"):
            return response.content
        if response.content.startswith(b"\xff\xd8\xff"):
            return response.content
        return None

    def pairing_code(self, phone):
        if self.session().get("status") != "SCAN_QR_CODE":
            raise ValueError("Сначала запустите подключение WhatsApp и дождитесь готовности.")
        response = self.http.post(AUTH + "/request-code", json={"phoneNumber": phone}, timeout=15)
        response.raise_for_status()
        code = response.json().get("code")
        if not isinstance(code, str) or not re.fullmatch(r"[A-Z0-9]{4}-?[A-Z0-9]{4}", code):
            raise ValueError("WhatsApp не выдал код. Попробуйте ещё раз или выберите QR.")
        return code if "-" in code else f"{code[:4]}-{code[4:]}"

    def pages(self, path, **options):
        offset = 0
        previous = None
        while True:
            items = self.get(path, limit=PAGE, offset=offset, **options)
            if isinstance(items, dict):
                items = list(items.values())
            if not isinstance(items, list):
                raise ValueError("WhatsApp вернул неожиданный список. Повторите попытку.")
            if not items:
                return
            keys = [item.get("id") or item.get("JID") for item in items]
            if keys == previous:
                raise ValueError("WhatsApp повторил страницу истории. Повторите выгрузку.")
            yield items
            if len(items) < PAGE:
                return
            previous = keys
            offset += len(items)

    def catalog(self):
        account = self.account()
        catalog = {}
        for page in self.pages(CHATS, sortBy="id", sortOrder="asc"):
            for item in page:
                cid = jid(item.get("id"))
                if cid.endswith(SUFFIXES):
                    catalog[cid] = {"id": cid, "name": item.get("name") or cid.split("@")[0],
                                    "kind": "group" if cid.endswith("@g.us") else "direct"}
        # GOWS can know about a group before any of its messages reach the linked device.
        try:
            groups = self.get("/api/default/groups", exclude="participants")
        except httpx.HTTPError:
            groups = []
        for item in (groups.values() if isinstance(groups, dict) else groups):
            cid = jid(item.get("JID") or item.get("id"))
            if cid.endswith("@g.us") and not item.get("IsParent"):
                catalog[cid] = {"id": cid, "name": item.get("Name") or item.get("name") or cid.split("@")[0],
                                "kind": "group"}
            elif item.get("IsParent"):
                catalog.pop(cid, None)
        return account, sorted(catalog.values(), key=lambda item: (item["name"].casefold(), item["id"]))

    def messages(self, chat, start, through):
        params = {"sortBy": "timestamp", "sortOrder": "asc", "downloadMedia": "false",
                  "filter.timestamp.lte": through}
        if start:
            params["filter.timestamp.gte"] = start
        return self.pages(f"{CHATS}/{quote(chat, safe='')}/messages", **params)

    def attachment(self, state, chat, message):
        media = message.get("media") or {}
        item = {"filename": media.get("filename"), "mime": media.get("mimetype"), "status": "unavailable"}
        path = f"{CHATS}/{quote(chat, safe='')}/messages/{quote(message['id'], safe='')}"
        try:
            detail = self.get(path, downloadMedia="true")
            media = detail.get("media") or {}
            item["filename"] = media.get("filename") or item["filename"]
            item["mime"] = media.get("mimetype") or item["mime"]
            parts = urlsplit(media.get("url") or "")
            if not unquote(parts.path).startswith("/api/files/") or ".." in unquote(parts.path).split("/"):
                return item
            with self.http.stream("GET", parts.path, params=parts.query) as response, tempfile.TemporaryFile() as file:
                response.raise_for_status()
                size = 0
                for chunk in response.iter_bytes(65536):
                    size += len(chunk)
                    if size > MAX_MEDIA:
                        return {**item, "status": "too_large"}
                    file.write(chunk)
                file.seek(0)
                return {**item, "status": "downloaded", "size": size, "digest": save_blob(state, file)}
        except httpx.HTTPError as exc:
            if isinstance(exc, httpx.HTTPStatusError) and exc.response.status_code in (404, 410):
                return item
            return {**item, "status": "retry"}


def short_id(value):
    if isinstance(value, str):
        parts = value.split("_")
        return parts[2] if len(parts) >= 3 and parts[0] in ("true", "false") else value
    return None


def from_waha(message, chat, attachments):
    data = message.get("_data") or {}
    info = data.get("Info") or {}
    content = data.get("Message") or {}
    key = short_id(info.get("ID") or message.get("id"))
    author = info.get("PushName")
    mine = bool(message.get("fromMe", info.get("IsFromMe")))
    mime = (attachments[0].get("mime") or "") if attachments else ""
    kind = ("audio" if mime.startswith("audio/") or "audioMessage" in content
            else mime.split("/")[0] if mime.startswith(("image/", "video/"))
            else "document" if attachments else "text")
    return {
        "chat_id": chat["id"], "chat": chat["name"], "message_id": key, "source": "waha",
        "author": None if mine else author or (chat["name"] if chat["kind"] == "direct" else None),
        "from_me": mine, "timestamp": utc_at(int(message["timestamp"])), "type": kind,
        "text": message.get("body"), "caption": None, "attachments": attachments,
        "reply_to": short_id((message.get("replyTo") or {}).get("id")),
        "edited": bool(message.get("edited")), "deleted": bool(message.get("deleted")),
    }


def import_chat(state, waha, chat, *, start, through, embedder, progress, target=None):
    stats = {"scanned": 0, "saved": 0, "unavailable": 0, "latest": start}
    archive_chat = {**chat, **target} if target else chat
    for page in waha.messages(chat["id"], start, through):
        batch = []
        with database(state) as db:
            for message in page:
                if not message.get("timestamp") or not message.get("id"):
                    continue
                if target and not start <= int(message["timestamp"]) <= through:
                    continue
                record = from_waha(message, archive_chat, [])
                mid = identifier(archive_chat["id"], record["message_id"])
                if message.get("hasMedia") and not record["deleted"]:
                    old = db.execute("SELECT record FROM messages WHERE id=?", (mid,)).fetchone()
                    items = json.loads(old[0])["attachments"] if old else []
                    if not items or items[0].get("status") != "downloaded":
                        items = [waha.attachment(state, chat["id"], message)]
                    record = from_waha(message, archive_chat, items)
                    stats["unavailable"] += items[0].get("status") != "downloaded"
                if target:
                    record["source"] = "waha_incremental"
                    record["source_chat_id"] = chat["id"]
                batch.append((mid, record))
                stats["latest"] = max(stats["latest"], int(message["timestamp"]))
        with database(state, writable=True) as db:
            db.execute("BEGIN IMMEDIATE")
            changes = [(mid, record) for mid, record in batch if write_message(db, mid, record)]
            reindex(db, changes, embedder)
            if target:
                db.executemany("UPDATE messages SET active=1 WHERE id=? AND active=0",
                               ((mid,) for mid, _ in batch))
        stats["scanned"] += len(batch)
        stats["saved"] += len(changes)
        progress(stats)
    return stats
