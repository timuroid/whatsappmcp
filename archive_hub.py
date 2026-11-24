"""Connect, select, and synchronize the archive from the local panel."""

import json
import os
import sqlite3
import subprocess
import threading
import time
import unicodedata
from contextlib import contextmanager, suppress
from datetime import datetime

import httpx

from archive import DISCARDED_BLOBS, database, get_meta, prepare_index, purge_discarded_blobs, put_meta, remove_chat_records
from backfill import backfill, pair_device
from retrieval import configured_embedder
from waha_source import OVERLAP, RESCAN, Waha, import_chat, initialize_waha, jid, utc_at

DEFAULT_INTERVAL = 86400
INTERVAL = int(os.environ.get("WA_HISTORY_SYNC_INTERVAL_SECONDS", str(DEFAULT_INTERVAL)))
if INTERVAL < 1:
    raise ValueError("WA_HISTORY_SYNC_INTERVAL_SECONDS должен быть положительным целым числом.")
POLL_INTERVAL = min(60, max(1, INTERVAL // 10))
RETRY = 900
AUTO_LINKS = "automatic_updates"
AUTO_MATCHES = "automatic_matches"
MATCH_PENDING = {"waiting", "unverified"}
MATCH_REVIEW = {"ambiguous", "not_found", "conflict"}


def read_state(state, key):
    if not (state / "archive.sqlite3").is_file():
        return None
    with database(state) as db:
        return get_meta(db, key)


def next_update_at(link):
    deadline = link["next_at"]
    last = link.get("last")
    if last and not link.get("error") and deadline >= last["at"] + link.get("interval", DEFAULT_INTERVAL):
        return last["at"] + INTERVAL
    return deadline


def import_selected(state, waha, chats, account, *, full, embedder, progress):
    if waha.account() != account:
        raise ValueError("Аккаунт изменился. Обновите список чатов.")
    through = int(time.time())
    with database(state, writable=True) as db:
        db.execute("BEGIN IMMEDIATE")
        if get_meta(db, "account") not in (None, account):
            raise ValueError("Этот архив связан с другим аккаунтом WhatsApp.")
        prepare_index(db, embedder)
        put_meta(db, "account", account)
        put_meta(db, "live_selection", chats)
        put_meta(db, "live_attempt", through)
        db.execute("""UPDATE messages SET active=(chat_id IN (SELECT value FROM json_each(?)))
                      WHERE id NOT IN (SELECT id FROM manual_messages)
                            AND json_extract(record, '$.source') IS NOT 'waha_incremental'""",
                   (json.dumps([chat["id"] for chat in chats]),))
    result = {"scanned": 0, "saved": 0, "unavailable": 0}
    for index, chat in enumerate(chats, 1):
        progress({**result, "current": index, "total": len(chats), "chat": chat["name"]})
        previous = read_state(state, "live_history:" + chat["id"]) or {}
        deep = full or not previous.get("latest") or through - previous.get("full", 0) >= RESCAN
        start = 0 if deep else max(0, previous["latest"] - OVERLAP)

        def page_stats(stats):
            progress({**{key: result[key] + stats[key] for key in result},
                      "current": index, "total": len(chats), "chat": chat["name"]})

        stats = import_chat(state, waha, chat, start=start, through=through,
                            embedder=embedder, progress=page_stats)
        with database(state, writable=True) as db:
            put_meta(db, "live_history:" + chat["id"], {
                "latest": max(previous.get("latest", 0), stats["latest"]),
                "full": through if deep else previous.get("full", 0),
            })
        for key in result:
            result[key] += stats[key]
    with database(state, writable=True) as db:
        put_meta(db, "live_result", {**result, "at": through})
        put_meta(db, "sync_at", utc_at(through))
        put_meta(db, "source_sync_ok", True)
    return result


def match_name(name):
    return " ".join(unicodedata.normalize("NFKC", name).casefold().split())


def queue_match(state, target, title):
    with database(state, writable=True) as db:
        db.execute("BEGIN IMMEDIATE")
        if target in (get_meta(db, AUTO_LINKS) or {}):
            return "linked"
        pending = get_meta(db, AUTO_MATCHES) or {}
        match = {"title": title, "status": "waiting"}
        if pending.get(target) != match:
            pending[target] = match
            put_meta(db, AUTO_MATCHES, pending)
        return "waiting"


def save_automatic_link(state, target, chat, account, *, group=False, replace=False,
                        review_only=False, expected_match=None):
    if group and (chat["kind"] != "group" or target != chat["id"]):
        raise ValueError("Выберите группу WhatsApp.")
    with database(state, writable=True) as db:
        db.execute("BEGIN IMMEDIATE")
        if get_meta(db, "account") not in (None, account):
            raise ValueError("Этот архив связан с другим аккаунтом WhatsApp.")
        links = get_meta(db, AUTO_LINKS) or {}
        pending = get_meta(db, AUTO_MATCHES) or {}
        if expected_match is not None and pending.get(target) != expected_match:
            raise ValueError("Архив обновился во время подбора чата.")
        if review_only and (pending.get(target) or {}).get("status") not in MATCH_REVIEW:
            raise ValueError("Выбор источника доступен для архива, который требует проверки.")
        previous = links.get(target)
        other_target = next((key for key, item in links.items()
                             if key != target and item["source"]["id"] == chat["id"]), None)
        legacy_direct = other_target == chat["id"] and links[other_target].get("mode") == "live"
        if other_target is not None and (group or not legacy_direct):
            raise ValueError("У этого чата уже есть привязка. Проверьте список автообновлений.")
        if previous:
            if previous["source"]["id"] == chat["id"] and (group or replace):
                return
            if not replace or previous.get("mode") == "group" or target == previous["source"]["id"]:
                raise ValueError("У этого чата уже есть привязка к WhatsApp.")
        if group:
            name, floor = chat["name"], 0
        else:
            anchor = db.execute("""SELECT timestamp, json_extract(record, '$.chat') AS name
                                   FROM messages JOIN manual_messages USING(id)
                                   WHERE chat_id=? AND active=1 AND deleted=0
                                   ORDER BY timestamp DESC LIMIT 1""", (target,)).fetchone()
            if not anchor:
                raise ValueError("Сначала импортируйте файл с телефона в этот чат.")
            cutoff = int(datetime.fromisoformat(anchor["timestamp"]).timestamp())
            if cutoff > time.time():
                raise ValueError("Последнее сообщение в файле датировано будущим. Проверьте формат даты и часовой пояс.")
            name, floor = anchor["name"], cutoff + 1
        if previous:
            # Keep the imported file. Exclude messages fetched from the former source.
            db.execute("""UPDATE messages SET active=0 WHERE chat_id=?
                          AND json_extract(record, '$.source')='waha_incremental'""", (target,))
        if legacy_direct:
            # Move the schedule. Messages already collected in the old chat stay readable.
            del links[other_target]
        links[target] = {"target": {"id": target, "name": name}, "source": chat,
                         "mode": "group" if group else "export",
                         "floor": floor, "enabled": True, "next_at": int(time.time())}
        put_meta(db, "account", account)
        put_meta(db, AUTO_LINKS, links)
        if pending.pop(target, None) is not None:
            put_meta(db, AUTO_MATCHES, pending)


def resolve_matches(state, account, catalog):
    by_name = {}
    for chat in catalog:
        by_name.setdefault(match_name(chat["name"]), []).append(chat)
    linked = []
    for target, match in (read_state(state, AUTO_MATCHES) or {}).items():
        if match["status"] not in MATCH_PENDING:
            continue
        chats = by_name.get(match_name(match["title"]), [])
        if len(chats) == 1:
            try:
                save_automatic_link(state, target, chats[0], account, expected_match=match)
                linked.append(target)
                continue
            except ValueError:
                result = "conflict"
        else:
            result = "ambiguous" if chats else "not_found"
        with database(state, writable=True) as db:
            db.execute("BEGIN IMMEDIATE")
            pending = get_meta(db, AUTO_MATCHES) or {}
            if pending.get(target) == match:
                pending[target]["status"] = result
                put_meta(db, AUTO_MATCHES, pending)
    return linked


def run_automatic(state, waha, links, *, embedder, progress):
    if waha.account() != read_state(state, "account"):
        raise ValueError("Подключите WhatsApp-аккаунт, который использовался для привязки чатов.")
    through = int(time.time())
    with database(state, writable=True) as db:
        prepare_index(db, embedder)
    for current, link in enumerate(links, 1):
        details = {"current": current, "total": len(links), "chat": link["target"]["name"]}

        def report(stats):
            progress({**details, **stats})

        report({})
        full = link.get("mode") in ("group", "live") and through - link.get("full_at", 0) >= RESCAN
        start = link["floor"] if full else max(link["floor"], link.get("through", link["floor"]) - OVERLAP)
        try:
            stats = import_chat(state, waha, link["source"], start=start, through=through,
                                embedder=embedder, progress=report, target=link["target"])
        except (httpx.HTTPError, OSError, ValueError, sqlite3.Error):
            changes = {"error": "Обновление прервалось. Попробуем снова через 15 минут.",
                       "next_at": int(time.time()) + RETRY}
        else:
            finished = int(time.time())
            changes = {"through": through, "error": None, "last": {**stats, "at": finished},
                       "interval": INTERVAL, "next_at": finished + INTERVAL}
            if full and stats["scanned"]:
                changes["full_at"] = through
        with database(state, writable=True) as db:
            db.execute("BEGIN IMMEDIATE")
            saved = get_meta(db, AUTO_LINKS)
            saved[link["target"]["id"]].update(changes)
            put_meta(db, AUTO_LINKS, saved)


class ArchiveHub:
    def __init__(self, *, personal=False):
        self.personal = personal
        self.guard = threading.Lock()
        self.manual_import = threading.Lock()
        self.job = None
        self.error = None
        self.error_job = None
        self.data = {}
        self.catalog_account = None
        self.catalog_chats = {}
        self.peer_qr = None
        self.retry_updates_at = 0
        self.retry_matching_at = 0

    def connector(self, state):
        return Waha(state, personal=self.personal)

    @contextmanager
    def idle_archive(self):
        with self.guard:
            if self.job or not self.manual_import.acquire(blocking=False):
                raise ValueError("Дождитесь окончания импорта или синхронизации и повторите удаление.")
            try:
                yield
            finally:
                self.manual_import.release()

    def delete_chat(self, state, chat_id):
        if not (state / "archive.sqlite3").is_file():
            return None
        with self.idle_archive():
            with database(state, writable=True) as db:
                db.execute("PRAGMA secure_delete=ON")
                db.execute("BEGIN IMMEDIATE")
                if not db.execute("SELECT 1 FROM messages WHERE chat_id=? LIMIT 1", (chat_id,)).fetchone():
                    return None
                remove_chat_records(db, chat_id)
                for key in (AUTO_LINKS, AUTO_MATCHES):
                    entries = get_meta(db, key) or {}
                    if entries.pop(chat_id, None) is not None:
                        put_meta(db, key, entries)
                selection = get_meta(db, "live_selection") or []
                remaining = [chat for chat in selection if chat["id"] != chat_id]
                if len(remaining) != len(selection):
                    put_meta(db, "live_selection", remaining)
            self.data = {}
            # Commit before touching files. A failed unlink can be retried by poll().
            try:
                pending = purge_discarded_blobs(state) > 0
            except sqlite3.Error:
                pending = True
            return {"pending_files": pending}

    def launch(self, kind, fn):
        with self.guard:
            if self.job:
                raise ValueError("Дождитесь окончания текущей операции.")
            self.job, self.error, self.error_job, self.data = kind, None, None, {}

        def run():
            try:
                fn()
            except ValueError as exc:
                self.error = str(exc)
            except subprocess.SubprocessError:
                self.error = ("Не удалось запустить подключение. Проверьте Docker Desktop."
                              if kind == "connect" else "Сборщик прервал работу. Попробуйте ещё раз.")
            except (httpx.HTTPError, OSError, sqlite3.Error):
                self.error = ("Не удалось связаться с WhatsApp. Проверьте Docker Desktop."
                              if kind == "connect" else "Операция прервалась. Сохранённые сообщения доступны в архиве.")
            except Exception:
                self.error = "Операция прервалась. Повторите попытку."
            finally:
                with self.guard:
                    self.error_job = kind if self.error else None
                    if kind == "automatic":
                        self.retry_updates_at = time.time() + RETRY if self.error else 0
                    if kind == "matching":
                        self.retry_matching_at = time.time() + RETRY if self.error else 0
                    self.job = None

        threading.Thread(target=run, daemon=True).start()

    def connect(self, state, *, renew_qr=False, reconnect=False):
        def start():
            initialize_waha(state, personal=self.personal)
            for _ in range(15):
                try:
                    with self.connector(state) as waha:
                        if reconnect:
                            waha.reconnect()
                            with self.guard:
                                self.catalog_account = None
                                self.catalog_chats.clear()
                        else:
                            waha.connect(renew_qr=renew_qr)
                    return
                except httpx.TransportError:
                    time.sleep(1)
            raise ValueError("Сервис WhatsApp ещё запускается. Нажмите «Подключить WhatsApp» ещё раз.")

        self.launch("connect", start)

    def status(self, state):
        status = "DISCONNECTED"
        if (state / "waha.key").is_file():
            try:
                with self.connector(state) as waha:
                    session = waha.session()
                    status = session.get("status", status)
                    identity = jid((session.get("me") or {}).get("id")).replace("@s.whatsapp.net", "@c.us")
                    if status == "WORKING" and identity and read_state(state, "account") not in (None, identity):
                        status = "ACCOUNT_MISMATCH"
            except (httpx.HTTPError, OSError):
                pass
        return {"connection": status, "job": self.job, "progress": self.data,
                "error": self.error, "error_job": self.error_job,
                "last": read_state(state, "live_result"),
                "saved_chats": len(read_state(state, "live_selection") or []),
                "last_attempt": read_state(state, "live_attempt"),
                "updates": [{**link, "next_at": next_update_at(link)}
                            for link in (read_state(state, AUTO_LINKS) or {}).values()],
                "matches": read_state(state, AUTO_MATCHES) or {},
                "updates_retry_at": self.retry_updates_at,
                "backfill_available": (state / "wa-peer").is_file(),
                "backfill_qr": self.peer_qr is not None,
                "backfill_last": read_state(state, "backfill_result"),
                "backfill_resume_at": read_state(state, "backfill_resume_at")}

    def fetch_catalog(self, state, waha):
        account, items = waha.catalog()
        if read_state(state, "account") not in (None, account):
            raise ValueError("Этот архив связан с другим аккаунтом WhatsApp.")
        with self.guard:
            self.catalog_account = account
            self.catalog_chats = {item["id"]: item for item in items}
        return account, items

    def catalog(self, state):
        with self.connector(state) as waha:
            _, items = self.fetch_catalog(state, waha)
        selected = {chat["id"] for chat in read_state(state, "live_selection") or []}
        links = {link["source"]["id"]: link for link in (read_state(state, AUTO_LINKS) or {}).values()
                 if link.get("mode") != "live"}
        return [{**item, "selected": item["id"] in selected,
                 "subscription": ("active" if links[item["id"]]["enabled"] else "paused")
                 if item["id"] in links else None} for item in items]

    def select(self, state, ids, *, full=False):
        if not ids or any(key not in self.catalog_chats for key in ids):
            raise ValueError("Выберите чаты из актуального списка.")
        chats = [self.catalog_chats[key] for key in dict.fromkeys(ids)]
        self.sync(state, chats, self.catalog_account, full=full)

    def sync(self, state, chats, account, *, full=False):
        def run():
            with self.connector(state) as waha:
                import_selected(state, waha, chats, account, full=full,
                                embedder=configured_embedder(), progress=self.set_progress)

        self.launch("sync", run)

    def link_automatic(self, state, target, source, *, group=False, replace=False, review_only=False):
        with self.guard:
            if self.job:
                raise ValueError("Дождитесь окончания текущей операции.")
            if source not in self.catalog_chats:
                raise ValueError("Обновите список чатов WhatsApp и выберите чат снова.")
            save_automatic_link(state, target, self.catalog_chats[source], self.catalog_account,
                                group=group, replace=replace, review_only=review_only)

    def match_pending(self, state):
        def run():
            with self.connector(state) as waha:
                account, items = self.fetch_catalog(state, waha)
                targets = resolve_matches(state, account, items)
                if targets:
                    links = read_state(state, AUTO_LINKS)
                    run_automatic(state, waha, [links[target] for target in targets],
                                  embedder=configured_embedder(), progress=self.set_progress)

        self.launch("matching", run)

    def pause_automatic(self, state, target, paused):
        with self.guard:
            if self.job:
                raise ValueError("Дождитесь окончания текущей операции.")
            with database(state, writable=True) as db:
                db.execute("BEGIN IMMEDIATE")
                links = get_meta(db, AUTO_LINKS) or {}
                if target not in links:
                    raise ValueError("Привязка не найдена. Обновите страницу.")
                links[target]["enabled"] = not paused
                if not paused:
                    links[target]["next_at"] = int(time.time())
                put_meta(db, AUTO_LINKS, links)

    def update(self, state, *, due_only=False):
        def scheduled_links():
            return [link for link in (read_state(state, AUTO_LINKS) or {}).values()
                    if link["enabled"] and (not due_only or next_update_at(link) <= time.time())]

        if not due_only and not scheduled_links():
            raise ValueError("Включите автообновление хотя бы для одного чата.")

        def run():
            links = scheduled_links()
            if not links:
                return
            with self.connector(state) as waha:
                run_automatic(state, waha, links, embedder=configured_embedder(), progress=self.set_progress)

        self.launch("automatic", run)

    def set_progress(self, data):
        self.data = data

    def deep_history(self, state, *, allow_pair=True):
        binary = state / "wa-peer"
        store = state / "peer-device.sqlite3"
        if not binary.is_file():
            raise ValueError("Сборщик старой истории ещё не установлен.")
        if not read_state(state, "live_selection"):
            raise ValueError("Сначала выгрузите выбранные чаты.")

        def run():
            try:
                if allow_pair:
                    self.set_progress({"status": "pairing"})
                    pair_device(state, store, binary, self.set_peer_qr)
                self.peer_qr = None
                with database(state, writable=True) as db:
                    put_meta(db, "backfill_resume_at", int(time.time()) + 3600)
                reports = backfill(state, store, binary, configured_embedder(),
                                   progress=self.set_progress)
                retry = any(item["status"] in ("timeout", "request_failed") for item in reports)
                with database(state, writable=True) as db:
                    put_meta(db, "backfill_resume_at", int(time.time()) + 3600 if retry else None)
            finally:
                self.peer_qr = None

        self.launch("backfill", run)

    def set_peer_qr(self, value):
        self.peer_qr = value

    def poll(self, state, *, preload=False):
        if self.job:
            return
        if read_state(state, DISCARDED_BLOBS):
            with suppress(ValueError), self.idle_archive():
                purge_discarded_blobs(state)
        if not (state / "waha.key").is_file():
            return
        links = read_state(state, AUTO_LINKS) or {}
        now = time.time()
        matches_due = now >= self.retry_matching_at and (
            (preload and self.catalog_account is None) or any(
                value["status"] in MATCH_PENDING for value in (read_state(state, AUTO_MATCHES) or {}).values()
            )
        )
        updates_due = now >= self.retry_updates_at and any(
            item["enabled"] and next_update_at(item) <= now for item in links.values()
        )
        if matches_due or updates_due:
            try:
                if self.status(state)["connection"] == "WORKING":
                    if matches_due:
                        self.match_pending(state)
                    else:
                        self.update(state, due_only=True)
            except ValueError:
                pass
