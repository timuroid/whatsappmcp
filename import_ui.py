"""Local administration for old WhatsApp exports, separate from the MCP port."""

import asyncio
import json
import os
import re
import sqlite3
from contextlib import asynccontextmanager, suppress
from datetime import datetime
from functools import lru_cache
from pathlib import Path
from urllib.parse import urlsplit
from zoneinfo import ZoneInfo

import httpx
import uvicorn
from jinja2 import Environment, FileSystemLoader, select_autoescape
from starlette.applications import Starlette
from starlette.background import BackgroundTask
from starlette.concurrency import run_in_threadpool
from starlette.datastructures import UploadFile
from starlette.exceptions import HTTPException
from starlette.middleware import Middleware
from starlette.middleware.trustedhost import TrustedHostMiddleware
from starlette.responses import FileResponse, HTMLResponse, JSONResponse, RedirectResponse, Response, StreamingResponse
from starlette.routing import Route

from admin_access import ADMIN_LOGIN, COOKIE, SESSION_SECONDS, accepts_key, initialize_admin, is_admin, issue_session
from archive import STATE, account_state, attachment_blob, database, get_meta, index_pending_vectors
from archive_hub import (
    AUTO_LINKS, AUTO_MATCHES, MATCH_PENDING, MATCH_REVIEW, POLL_INTERVAL, ArchiveHub, queue_match, read_state,
)
from manual_import import MAX_UPLOAD, import_file
from plugin_access import AccessStore, LOCAL_ORIGIN, USER_COOKIE, USER_SESSION_SECONDS, mcp_origin, user_name
from retrieval import EmbeddingUnavailable, configured_embedder, history_page
from ui_assets import UI, asset

templates = Environment(loader=FileSystemLoader(UI), autoescape=select_autoescape(["html", "html.jinja"]))
STATE = STATE.expanduser().resolve()
hub = ArchiveHub()
PRIVATE = {"Cache-Control": "no-store", "X-Content-Type-Options": "nosniff"}
PAGE_HEADERS = {
    **PRIVATE,
    "Content-Security-Policy": "default-src 'none'; style-src 'self'; font-src 'self'; script-src 'self'; "
                               "connect-src 'self'; img-src 'self'; "
                               "form-action 'self'; frame-ancestors 'none'; base-uri 'none'",
    "Referrer-Policy": "same-origin",
}


@lru_cache(maxsize=None)
def personal_hub(state):
    return ArchiveHub(personal=True)


def source_hub(state):
    return hub if state == STATE else personal_hub(state)


@asynccontextmanager
async def lifespan(_app):
    await run_in_threadpool(initialize_admin, STATE)
    remote_embedder = configured_embedder() if os.environ.get("WA_HISTORY_EMBED_PROVIDER") == "openai" else None

    async def states():
        users = await run_in_threadpool(AccessStore(STATE).users)
        return [STATE, *(account_state(STATE, user["username"]) for user in users
                         if user["enabled"] and user["username"] != ADMIN_LOGIN)]

    async def schedule():
        async def poll_archive(state):
            with suppress(sqlite3.Error):
                await run_in_threadpool(source_hub(state).poll, state)

        while True:
            with suppress(sqlite3.Error):
                await asyncio.gather(*(poll_archive(state) for state in await states()))
            await asyncio.sleep(POLL_INTERVAL)

    async def index_embeddings():
        while True:
            processed = 0
            delay = 10
            try:
                for state in await states():
                    with suppress(sqlite3.Error):
                        processed += await run_in_threadpool(index_pending_vectors, state, remote_embedder)
                if processed:
                    delay = 1
            except (EmbeddingUnavailable, sqlite3.Error):
                delay = 30
            await asyncio.sleep(delay)

    tasks = [asyncio.create_task(schedule())]
    if remote_embedder:
        tasks.append(asyncio.create_task(index_embeddings()))
    try:
        yield
    finally:
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)


def chat_list(state=None):
    state = STATE if state is None else state
    if not (state / "archive.sqlite3").exists():
        return []
    with database(state) as db:
        links = get_meta(db, AUTO_LINKS) or {}
        matches = get_meta(db, AUTO_MATCHES) or {}
        rows = db.execute("""
            SELECT chat_id, COALESCE(MAX(json_extract(record, '$.chat')), 'Чат без названия') AS name,
                   COUNT(*) AS count, MIN(timestamp) AS first, MAX(timestamp) AS last,
                   MAX(CASE WHEN id IN (SELECT id FROM manual_messages) THEN timestamp END) AS imported_through,
                   SUM((SELECT COUNT(*) FROM json_each(messages.record, '$.attachments') AS item
                        WHERE json_extract(item.value, '$.status') = 'downloaded')) AS attachments,
                   SUM((SELECT COUNT(*) FROM json_each(messages.record, '$.attachments') AS item
                        WHERE json_extract(item.value, '$.status') IS NOT 'downloaded')) AS unavailable
            FROM messages WHERE active=1 AND deleted=0
            GROUP BY chat_id ORDER BY name, chat_id
        """).fetchall()
        items = []
        for row in rows:
            link = links.get(row["chat_id"])
            match = matches.get(row["chat_id"])
            update_status = ("review" if link.get("error") else "scheduled" if link["enabled"] else "paused"
                             ) if link else "waiting" if match and match["status"] == "waiting" else "review" if match else None
            items.append({**dict(row), "sync_status": update_status,
                          "shared_group": bool(link and link["source"]["kind"] == "group")})
        return items


def member_chats(state):
    if not (state / "archive.sqlite3").exists():
        return []
    with database(state) as db:
        links = get_meta(db, AUTO_LINKS) or {}
        matches = get_meta(db, AUTO_MATCHES) or {}
        rows = db.execute("""
            SELECT chat_id, COALESCE(MAX(json_extract(record, '$.chat')), 'Чат без названия') AS name,
                   COUNT(*) AS count, MIN(timestamp) AS first
            FROM messages WHERE active=1 AND deleted=0
            GROUP BY chat_id ORDER BY name, chat_id
        """)
        moscow = ZoneInfo("Europe/Moscow")
        chats = []
        for row in rows:
            chat_id = row["chat_id"]
            link = links.get(chat_id)
            match = (matches.get(chat_id) or {}).get("status")
            if link:
                status = ("paused" if not link["enabled"] else "retry" if link.get("error")
                          else "scheduled" if link.get("last") else "updating")
            else:
                status = "waiting" if match in MATCH_PENDING else match
            chat = {"chat_id": chat_id, "name": row["name"], "count": row["count"],
                    "first_year": datetime.fromisoformat(row["first"]).astimezone(moscow).year}
            if status:
                chat["sync_status"] = status
            chats.append(chat)
        return chats


def selection_archive_summary(state):
    if not (state / "archive.sqlite3").exists():
        return None
    with database(state) as db:
        selection = get_meta(db, "live_selection")
        if not selection:
            return None
        return dict(db.execute("""
            SELECT COUNT(*) AS messages, MIN(timestamp) AS first, MAX(timestamp) AS last,
                   COALESCE(SUM((
                       SELECT COUNT(*) FROM json_each(messages.record, '$.attachments') AS item
                       WHERE json_extract(item.value, '$.status') = 'downloaded'
                   )), 0) AS attachments
            FROM messages
            WHERE active=1 AND deleted=0 AND chat_id IN (SELECT value FROM json_each(?))
        """, (json.dumps([chat["id"] for chat in selection]),)).fetchone())


def page(*, result=None, error=None, form=None, status=200, view="home", admin=False, owner=ADMIN_LOGIN):
    state = account_state(STATE, owner)
    chats = chat_list(state) if admin else member_chats(state)
    return HTMLResponse(templates.get_template("archive.html.jinja").render(
        result=result, error=error, chats=chats, form=form or {},
        view=view, admin=admin, member=None if owner == ADMIN_LOGIN else owner,
        admin_session=admin or owner == ADMIN_LOGIN, owner=owner,
        archive_users=AccessStore(STATE).users() if admin else [],
    ), status_code=status, headers=PAGE_HEADERS)


def require_member(request):
    if is_admin(request, STATE):
        return ADMIN_LOGIN
    _, username = AccessStore(STATE).web_access(request.cookies.get(USER_COOKIE))
    if not username:
        raise HTTPException(401)
    return username


def home(request):
    try:
        member = require_member(request)
    except HTTPException:
        return RedirectResponse("/login", status_code=303, headers=PRIVATE)
    return page(owner=member)


def require_origin(request):
    if request.headers.get("origin") != f"{request.url.scheme}://{request.url.netloc}":
        raise HTTPException(403)


def require_admin(request):
    if not is_admin(request, STATE):
        raise HTTPException(403, detail="Войдите как администратор.")


def request_owner(request, *, admin=False):
    selected = request.query_params.get("owner")
    if not admin and selected is None:
        return require_member(request)
    require_admin(request)
    if not selected or selected == ADMIN_LOGIN:
        return ADMIN_LOGIN
    if not any(user["username"] == selected for user in AccessStore(STATE).users()):
        raise HTTPException(404, detail="Архив пользователя не найден.")
    return selected


def source_for(request, *, admin=False):
    state = account_state(STATE, request_owner(request, admin=admin))
    return state, source_hub(state)


def sign_in(*, error=None, status=200, username=""):
    return HTMLResponse(templates.get_template("sign-in.html.jinja").render(error=error, username=username),
                        status_code=status, headers=PAGE_HEADERS)


async def member_entry(request):
    store = AccessStore(STATE)
    if request.method == "GET":
        if is_admin(request, STATE):
            return RedirectResponse("/", status_code=303, headers=PRIVATE)
        _, member = await run_in_threadpool(store.web_access, request.cookies.get(USER_COOKIE))
        if member:
            return RedirectResponse("/", status_code=303, headers=PRIVATE)
        return sign_in()
    require_origin(request)
    if not request.headers.get("content-length", "").isdigit() or int(request.headers["content-length"]) > 4096:
        raise HTTPException(413)
    async with request.form(max_files=0, max_fields=2) as fields:
        username = user_name(fields.get("username"))
        password = fields.get("password")
    cookie_settings = {"httponly": True, "secure": request.url.scheme == "https", "samesite": "strict"}
    if username == ADMIN_LOGIN and isinstance(password, str) and accepts_key(STATE, password.strip()):
        response = RedirectResponse("/", status_code=303, headers=PRIVATE)
        response.set_cookie(COOKIE, issue_session(STATE), max_age=SESSION_SECONDS, **cookie_settings)
        response.delete_cookie(USER_COOKIE, **cookie_settings)
    else:
        token, status = await run_in_threadpool(store.web_login, username, password)
        if not token:
            error = "Повторите попытку через минуту." if status == 429 else "Проверьте логин и пароль."
            return sign_in(status=status, error=error, username=username[:32])
        response = RedirectResponse("/", status_code=303, headers=PRIVATE)
        response.set_cookie(USER_COOKIE, token, max_age=USER_SESSION_SECONDS, **cookie_settings)
        response.delete_cookie(COOKIE, **cookie_settings)
    await run_in_threadpool(store.web_logout, request.cookies.get(USER_COOKIE))
    return response


def member_logout(request):
    require_origin(request)
    AccessStore(STATE).web_logout(request.cookies.get(USER_COOKIE))
    response = RedirectResponse("/login", status_code=303, headers=PRIVATE)
    for cookie in (USER_COOKIE, COOKIE):
        response.delete_cookie(cookie, httponly=True, secure=request.url.scheme == "https", samesite="strict")
    return response


async def admin_entry(request):
    if is_admin(request, STATE):
        owner = await run_in_threadpool(request_owner, request, admin=True)
        return await run_in_threadpool(page, admin=True, view="home", owner=owner)
    _, member = await run_in_threadpool(AccessStore(STATE).web_access, request.cookies.get(USER_COOKIE))
    return RedirectResponse("/" if member else "/login", status_code=303, headers=PRIVATE)


async def upload(request):
    require_origin(request)
    admin = request.url.path == "/admin/import"
    owner = await run_in_threadpool(request_owner, request, admin=admin)
    state = account_state(STATE, owner)
    wants_json = not admin and request.headers.get("accept") == "application/json"
    values = {}

    async def respond(*, error=None, result=None, status=200):
        if wants_json:
            payload = {"error": error} if error else {"chat_id": result["chat_id"], "sync_status": result["sync_status"]}
            return JSONResponse(payload, status_code=status, headers=PRIVATE)
        return await run_in_threadpool(page, error=error, result=result, form=values,
                                       status=status, view="manual", admin=admin, owner=owner)

    length = request.headers.get("content-length", "")
    if not length.isdigit() or int(length) > MAX_UPLOAD + 64 * 1024:
        return await respond(error="Максимальный размер файла — 512 МБ.", status=413)
    try:
        async with request.form(max_files=1, max_fields=6, max_part_size=4096) as fields:
            values = {key: str(fields.get(key, "")) for key in ("name", "target", "timezone", "order", "owner")}
            file = fields.get("file")
            if not isinstance(file, UploadFile) or not file.filename or not file.size:
                raise ValueError("Выберите TXT или ZIP с телефона.")
            if file.size > MAX_UPLOAD:
                raise ValueError("Максимальный размер файла — 512 МБ.")
            result = await run_in_threadpool(
                lambda: import_file(
                    state, file.file, file.filename, name=values["name"],
                    target=values["target"] or None, timezone=values["timezone"],
                    order=values["order"], embedder=configured_embedder(),
                    require_new=fields.get("mode") == "new", owner=values["owner"],
                )
            )
    except ValueError as exc:
        return await respond(error=str(exc), status=422)
    except (OSError, sqlite3.Error, HTTPException):
        return await respond(error="Не удалось завершить импорт. Проверьте файл и свободное место.", status=422)
    try:
        result["sync_status"] = await run_in_threadpool(
            queue_match, state, result["chat_id"], result["source_title"] or result["chat"],
        )
    except sqlite3.Error:
        result["sync_status"] = "review"
    response = await respond(result=result)
    response.background = BackgroundTask(source_hub(state).poll, state)
    return response


def export(request):
    owner = request_owner(request, admin=True)
    state = account_state(STATE, owner)
    year = request.query_params.get("year", "").strip()
    chat_id = request.query_params.get("chat", "").strip()
    if year and (len(year) != 4 or not year.isascii() or not year.isdigit() or not 2000 <= int(year) <= 2099):
        return page(error="Введите год из диапазона 2000–2099.", status=422, view="archive", admin=True, owner=owner)
    filters = ["active=1", "deleted=0"]
    args = []
    if chat_id:
        filters.append("chat_id=?")
        args.append(chat_id)
    if year:
        filters.append("timestamp>=? AND timestamp<?")
        args.extend((f"{year}-01-01T00:00:00Z", f"{int(year)+1}-01-01T00:00:00Z"))
    sql = "SELECT record FROM messages WHERE " + " AND ".join(filters)
    if not (state / "archive.sqlite3").exists():
        return page(error="Сначала добавьте хотя бы один чат.", status=422, view="archive", admin=True, owner=owner)
    with database(state) as db:
        if not db.execute(sql + " LIMIT 1", args).fetchone():
            return page(error="За этот период в выбранных чатах нет сообщений.", status=422,
                        view="archive", admin=True, owner=owner)

    def rows():
        with database(state) as db:
            for row in db.execute(sql + " ORDER BY timestamp, id", args):
                record = json.loads(row["record"])
                record.setdefault("source", "wamvp")
                for attachment in record["attachments"]:
                    attachment.pop("digest", None)
                yield json.dumps(record, ensure_ascii=False) + "\n"

    return StreamingResponse(rows(), media_type="application/x-ndjson",
                             headers={"Content-Disposition": f'attachment; filename="whatsapp-{year or "all"}.jsonl"',
                                      "Cache-Control": "no-store", "X-Content-Type-Options": "nosniff"})


def source_status(request):
    state, archive_hub = source_for(request)
    status = archive_hub.status(state)
    background = (BackgroundTask(archive_hub.poll, state, preload=True)
                  if status["connection"] == "WORKING" and not status["job"] and archive_hub.catalog_account is None
                  else None)
    if is_admin(request, STATE):
        status["admin"] = True
    else:
        status = {"connection": status["connection"], "job": status["job"],
                  "error": status["error"], "error_job": status["error_job"],
                  "match_states": sorted(item["status"] for item in status["matches"].values()),
                  "updates": [{"enabled": item["enabled"], "last": item.get("last"), "error": item.get("error")}
                              for item in status["updates"]]}
    return JSONResponse(status, headers=PRIVATE, background=background)


def connect_source(request):
    state, archive_hub = source_for(request)
    require_origin(request)
    try:
        archive_hub.connect(state, renew_qr=request.url.path.endswith("/renew-qr"))
    except ValueError as exc:
        return JSONResponse({"error": str(exc)}, status_code=409, headers=PRIVATE)
    return JSONResponse({"started": True}, status_code=202, headers=PRIVATE)


async def reconnect_source(request):
    state, archive_hub = await run_in_threadpool(source_for, request)
    require_origin(request)
    if not request.headers.get("content-length", "").isdigit() or int(request.headers["content-length"]) > 1024:
        raise HTTPException(413)
    try:
        data = await request.json()
        if not isinstance(data, dict) or data.get("confirm") is not True:
            raise ValueError("Подтвердите переподключение.")
        await run_in_threadpool(archive_hub.connect, state, reconnect=True)
    except ValueError as exc:
        return JSONResponse({"error": str(exc)}, status_code=409, headers=PRIVATE)
    return JSONResponse({"started": True}, status_code=202, headers=PRIVATE)


def source_qr(request):
    state, archive_hub = source_for(request)
    try:
        with archive_hub.connector(state) as waha:
            image = waha.qr()
    except (OSError, httpx.HTTPError):
        image = None
    if not image:
        return Response(status_code=404, headers=PRIVATE)
    mime = "image/png" if image.startswith(b"\x89PNG") else "image/jpeg"
    return Response(image, media_type=mime, headers={**PRIVATE, "Cross-Origin-Resource-Policy": "same-origin"})


async def source_pair_code(request):
    state, archive_hub = await run_in_threadpool(source_for, request)
    require_origin(request)
    if not request.headers.get("content-length", "").isdigit() or int(request.headers["content-length"]) > 1024:
        raise HTTPException(413)
    try:
        data = await request.json()
        phone = data.get("phone", "") if isinstance(data, dict) else ""
        if not isinstance(phone, str) or not re.fullmatch(r"\+?[0-9 ()-]+", phone):
            raise ValueError("Введите номер WhatsApp с кодом страны.")
        digits = re.sub(r"[^0-9]", "", phone)
        if not re.fullmatch(r"[1-9][0-9]{7,14}", digits):
            raise ValueError("Введите номер WhatsApp с кодом страны.")

        def pair():
            with archive_hub.guard:
                if archive_hub.job:
                    raise ValueError("Дождитесь окончания подключения.")
                with archive_hub.connector(state) as waha:
                    return waha.pairing_code(digits)

        code = await run_in_threadpool(pair)
    except ValueError as exc:
        return JSONResponse({"error": str(exc)}, status_code=409, headers=PRIVATE)
    except (OSError, httpx.HTTPError):
        return JSONResponse({"error": "Код недоступен. Проверьте номер, повторите запрос или выберите QR."},
                            status_code=502, headers=PRIVATE)
    return JSONResponse({"code": code}, headers=PRIVATE)


def source_catalog(request):
    target = request.query_params.get("target")
    state, archive_hub = source_for(request, admin=target is None)
    try:
        if target is not None and ((read_state(state, AUTO_MATCHES) or {}).get(target) or {}).get("status") not in MATCH_REVIEW:
            raise ValueError("Выбор источника доступен для архива, который требует проверки.")
        items = archive_hub.catalog(state)
    except ValueError as exc:
        return JSONResponse({"error": str(exc)}, status_code=409, headers=PRIVATE)
    except (OSError, httpx.HTTPError):
        return JSONResponse({"error": "Не удалось получить список чатов. Обновите список."},
                            status_code=502, headers=PRIVATE)
    return JSONResponse({"chats": items}, headers=PRIVATE)


async def start_history(request):
    state, archive_hub = await run_in_threadpool(source_for, request, admin=True)
    require_origin(request)
    size = request.headers.get("content-length", "")
    if not size.isdigit() or int(size) > 2 * 1024 * 1024:
        raise HTTPException(413)
    try:
        payload = await request.json()
        ids = payload.get("chats") if isinstance(payload, dict) else None
        if not isinstance(ids, list) or not all(isinstance(cid, str) for cid in ids):
            raise ValueError("Выберите чаты из списка.")
        await run_in_threadpool(archive_hub.select, state, ids, full=payload.get("full") is True)
    except ValueError as exc:
        return JSONResponse({"error": str(exc)}, status_code=409, headers=PRIVATE)
    return JSONResponse({"started": True}, status_code=202, headers=PRIVATE)


def start_backfill(request):
    state, archive_hub = source_for(request, admin=True)
    require_origin(request)
    try:
        archive_hub.deep_history(state)
    except ValueError as exc:
        return JSONResponse({"error": str(exc)}, status_code=409, headers=PRIVATE)
    return JSONResponse({"started": True}, status_code=202, headers=PRIVATE)


def backfill_qr(request):
    _, archive_hub = source_for(request, admin=True)
    qr = archive_hub.peer_qr
    headers = {**PRIVATE, "Cross-Origin-Resource-Policy": "same-origin"}
    return Response(qr, status_code=200 if qr else 404, media_type="image/png", headers=headers)


def archive_overview(request):
    state = account_state(STATE, require_member(request))
    return JSONResponse({"chats": member_chats(state)}, headers=PRIVATE)


def admin_archive_overview(request):
    state = account_state(STATE, request_owner(request, admin=True))
    return JSONResponse({"chats": chat_list(state)}, headers=PRIVATE)


async def manage_users(request):
    require_admin(request)
    store = AccessStore(STATE)
    if request.method == "POST":
        require_origin(request)
        length = request.headers.get("content-length", "")
        if not length.isdigit() or int(length) > 8192:
            raise HTTPException(413)
        try:
            data = await request.json()
            if not isinstance(data, dict) or data.get("action") not in ("create", "enable", "disable", "password"):
                raise ValueError("Выберите действие для пользователя.")
            await run_in_threadpool(store.change_user, data["action"], data.get("username"),
                                    name=data.get("name"), password=data.get("password"))
        except ValueError as exc:
            return JSONResponse({"error": str(exc)}, status_code=422, headers=PRIVATE)
    return JSONResponse({"users": await run_in_threadpool(store.users)}, headers=PRIVATE)


def chat_history(request, state):
    chat = request.query_params.get("chat", "")
    query = request.query_params.get("query", "").strip()
    before = request.query_params.get("before", "")
    if not 1 <= len(chat) <= 256 or len(query) > 160 or (before and not re.fullmatch(r"[a-f0-9]{64}", before)):
        raise HTTPException(422)
    if not (state / "archive.sqlite3").exists():
        return JSONResponse({"messages": [], "next": None}, headers=PRIVATE)
    try:
        with database(state) as db:
            result = history_page(db, chat, before=before, query=query)
    except ValueError as exc:
        return JSONResponse({"error": str(exc)}, status_code=409, headers=PRIVATE)
    return JSONResponse(result, headers=PRIVATE)


def chat_attachment(request, state):
    message_id = request.path_params["message_id"]
    index = request.path_params["index"]
    if not re.fullmatch(r"[a-f0-9]{64}", message_id) or not (state / "archive.sqlite3").exists():
        raise HTTPException(404)
    with database(state) as db:
        row = db.execute("SELECT record FROM messages WHERE id=? AND active=1 AND deleted=0", (message_id,)).fetchone()
    attachments = json.loads(row["record"])["attachments"] if row else []
    if index >= len(attachments):
        raise HTTPException(404)
    item = attachments[index]
    path = attachment_blob(state, item)
    if path is None:
        raise HTTPException(404)
    filename = str(item.get("filename") or item.get("name") or "attachment").replace("\\", "/").rsplit("/", 1)[-1]
    return FileResponse(path, filename=filename, media_type="application/octet-stream", headers=PRIVATE)


def archive_read(handler, *, admin=False):
    def protected(request):
        owner = request_owner(request, admin=True) if admin else require_member(request)
        return handler(request, account_state(STATE, owner))

    return protected


def plugin_status(request):
    subject = require_member(request)
    try:
        origin = mcp_origin()
    except ValueError as exc:
        return JSONResponse({"error": str(exc)}, status_code=503, headers=PRIVATE)
    try:
        with httpx.Client(timeout=1.5, trust_env=False) as client:
            response = client.get(LOCAL_ORIGIN + "/.well-known/oauth-protected-resource/mcp")
            running = response.is_success and response.json().get("resource") == origin + "/mcp"
    except (httpx.HTTPError, ValueError):
        running = False
    return JSONResponse({**AccessStore(STATE).status(subject), "running": running,
                         "endpoint": origin + "/mcp", "local": origin == LOCAL_ORIGIN}, headers=PRIVATE)


async def plugin_action(request):
    require_origin(request)
    require_admin(request)
    length = request.headers.get("content-length", "")
    if not length.isdigit() or int(length) > 4096:
        raise HTTPException(413)
    action = request.path_params["action"]
    store = AccessStore(STATE)
    if action == "password":
        try:
            data = await request.json()
            await run_in_threadpool(store.set_admin_plugin_password,
                                    data.get("password") if isinstance(data, dict) else None)
        except ValueError as exc:
            return JSONResponse({"error": str(exc)}, status_code=422, headers=PRIVATE)
    elif action == "revoke":
        def invalidate():
            with store.db(write=True) as db:
                store.revoke(db, ADMIN_LOGIN)

        await run_in_threadpool(invalidate)
    else:
        raise HTTPException(404)
    return JSONResponse(await run_in_threadpool(store.status, ADMIN_LOGIN), headers=PRIVATE)


async def automatic_update(request):
    action = request.path_params["action"]
    state, archive_hub = await run_in_threadpool(source_for, request, admin=action not in ("run", "match"))
    require_origin(request)
    if action not in ("link", "match", "replace", "group", "pause", "resume", "run"):
        raise HTTPException(404)
    size = request.headers.get("content-length", "")
    if not size.isdigit() or int(size) > 8192:
        raise HTTPException(413)
    try:
        if action == "run":
            await run_in_threadpool(archive_hub.update, state)
        else:
            data = await request.json()
            if not isinstance(data, dict):
                raise ValueError("Выберите чат из архива.")
            if action in ("link", "match", "replace", "group"):
                source = data.get("source")
                target = source if action == "group" else data.get("target")
                if not isinstance(source, str) or not isinstance(target, str):
                    raise ValueError("Выберите чат WhatsApp.")
                await run_in_threadpool(archive_hub.link_automatic, state, target, source,
                                        group=action == "group", replace=action == "replace", review_only=action == "match")
            else:
                if not isinstance(data.get("target"), str):
                    raise ValueError("Выберите чат из архива.")
                await run_in_threadpool(archive_hub.pause_automatic, state, data["target"], action == "pause")
    except ValueError as exc:
        return JSONResponse({"error": str(exc)}, status_code=409, headers=PRIVATE)
    background = BackgroundTask(archive_hub.poll, state) if action in ("link", "match", "replace", "group", "resume") else None
    return JSONResponse({"started": True}, status_code=202, headers=PRIVATE, background=background)


def admin_hosts():
    hosts = ["127.0.0.1", "localhost"]
    remote = os.environ.get("WA_HISTORY_ADMIN_ORIGIN")
    if remote:
        url = urlsplit(remote)
        if url.scheme != "https" or not url.hostname or url.username or url.password or url.query or url.fragment or url.path not in ("", "/"):
            raise ValueError("WA_HISTORY_ADMIN_ORIGIN должен быть HTTPS-адресом без пути.")
        hosts.append(url.hostname)
    return hosts


app = Starlette(routes=[
    Route("/", home), Route("/import", upload, methods=["POST"]),
    Route("/login", member_entry, methods=["GET", "POST"]),
    Route("/logout", member_logout, methods=["POST"]),
    Route("/admin", admin_entry),
    Route("/admin/import", upload, methods=["POST"]),
    Route("/admin/logout", member_logout, methods=["POST"]),
    Route("/api/admin/users", manage_users, methods=["GET", "POST"]),
    Route("/api/admin/archive", admin_archive_overview),
    Route("/api/admin/history", archive_read(chat_history, admin=True)),
    Route("/api/admin/attachments/{message_id}/{index:int}", archive_read(chat_attachment, admin=True)),
    Route("/export", export), Route("/assets/{name}", asset),
    Route("/api/source", source_status),
    Route("/api/source/connect", connect_source, methods=["POST"]),
    Route("/api/source/reconnect", reconnect_source, methods=["POST"]),
    Route("/api/source/renew-qr", connect_source, methods=["POST"]),
    Route("/api/source/qr", source_qr),
    Route("/api/source/pair-code", source_pair_code, methods=["POST"]),
    Route("/api/source/chats", source_catalog),
    Route("/api/source/sync", start_history, methods=["POST"]),
    Route("/api/source/backfill", start_backfill, methods=["POST"]),
    Route("/api/source/backfill/qr", backfill_qr),
    Route("/api/source/updates/{action}", automatic_update, methods=["POST"]),
    Route("/api/archive", archive_overview),
    Route("/api/archive/history", archive_read(chat_history)),
    Route("/api/archive/attachments/{message_id}/{index:int}", archive_read(chat_attachment)),
    Route("/api/plugin", plugin_status),
    Route("/api/plugin/{action}", plugin_action, methods=["POST"]),
], lifespan=lifespan, middleware=[Middleware(TrustedHostMiddleware, allowed_hosts=admin_hosts())])

if __name__ == "__main__":
    uvicorn.run(app, host="127.0.0.1", port=8081, access_log=False)
