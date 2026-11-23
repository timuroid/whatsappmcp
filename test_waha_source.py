import io
import tempfile
import time
import unittest
from contextlib import nullcontext
from datetime import datetime
from pathlib import Path
from unittest.mock import patch

import httpx
from starlette.testclient import TestClient

import import_ui
from archive import account_state, database, get_meta, put_meta
from archive_hub import (
    AUTO_LINKS, AUTO_MATCHES, ArchiveHub, import_selected, next_update_at, queue_match, read_state,
    resolve_matches, run_automatic, save_automatic_link,
)
from import_ui import selection_archive_summary
from manual_import import import_file
from plugin_access import AccessStore
from retrieval import lookup
from waha_source import AUTH, CHATS, SESSION, Waha


class SourceTest(unittest.TestCase):
    def test_existing_schedule_follows_interval_and_preserves_retry(self):
        daily = {"last": {"at": 100}, "next_at": 86500}
        with patch("archive_hub.INTERVAL", 30):
            self.assertEqual(next_update_at(daily), 130)
            self.assertEqual(next_update_at({**daily, "next_at": 109}), 109)
            self.assertEqual(next_update_at({**daily, "next_at": 1000, "error": "retry"}), 1000)
            self.assertEqual(next_update_at({"next_at": 101}), 101)
        with patch("archive_hub.INTERVAL", 86400):
            self.assertEqual(next_update_at({**daily, "interval": 30, "next_at": 130}), 86500)

    def test_member_imports_archive_and_resolves_only_failed_matches(self):
        account = "333@c.us"
        direct = {"id": "111@c.us", "name": "Поставки", "kind": "direct"}
        group = {"id": "222@g.us", "name": "Команда", "kind": "group"}
        other_group = {"id": "444@g.us", "name": "КОМАНДА", "kind": "group"}
        calls = []
        messages = [{"id": "after-export", "timestamp": int(time.time()) - 1800, "body": "Новое сообщение"}]

        class Connector:
            def account(self):
                return account

            def session(self):
                return {"status": "WORKING", "me": {"id": account}}

            def catalog(self):
                return account, [direct, group, other_group]

            def messages(self, chat, start, through):
                calls.append((chat, start))
                yield list(messages)

        def wait_for_collection(hub):
            deadline = time.monotonic() + 3
            while hub.job and time.monotonic() < deadline:
                time.sleep(.01)
            self.assertIsNone(hub.job)
            self.assertIsNone(hub.error)

        with tempfile.TemporaryDirectory() as work:
            root = Path(work)
            member = "test.user"
            login = {"username": member, "password": "Personal test password"}
            AccessStore(root).change_user("create", **login, name="Пользователь")
            state = account_state(root, member)
            state.mkdir(parents=True)
            (state / "waha.key").write_text("fixture")
            hub = import_ui.personal_hub(state)
            origin = "http://127.0.0.1:8081"
            headers = {"Origin": origin}
            with (
                patch.object(import_ui, "STATE", root),
                patch.object(hub, "connector", side_effect=lambda _: nullcontext(Connector())),
                TestClient(import_ui.app, base_url=origin, follow_redirects=False) as client,
            ):
                catalog = "/api/source/chats"
                match = "/api/source/updates/match"
                self.assertEqual(client.get(catalog, params={"target": "unknown"}).status_code, 401)
                self.assertEqual(client.post("/login", data=login, headers=headers).status_code, 303)
                self.assertEqual(client.get(catalog + "?owner=admin").status_code, 403)
                self.assertEqual(client.get(catalog).status_code, 403)
                self.assertEqual(client.get("/api/source").json()["connection"], "WORKING")
                wait_for_collection(hub)
                self.assertEqual(set(hub.catalog_chats), {direct["id"], group["id"], other_group["id"]})
                raw = "13.2.20, 9:00 - Анна: Старая история из файла".encode()
                upload = {**headers, "Accept": "application/json"}
                form = {"mode": "new", "order": "dmy", "timezone": "Europe/Moscow"}
                imported = client.post("/import", headers=upload, data={**form, "name": "Архив поставок"},
                                       files={"file": ("WhatsApp Chat - Поставки.txt", raw)})
                self.assertEqual(imported.status_code, 200, imported.text)
                direct_target = imported.json()["chat_id"]
                wait_for_collection(hub)
                self.assertEqual(calls, [(direct["id"], 1581573601)])
                self.assertEqual(client.get(catalog, params={"target": direct_target}).status_code, 409)
                self.assertEqual(client.post(match, json={"target": direct_target, "source": group["id"]},
                                             headers=headers).status_code, 409)
                history = client.get("/api/archive/history", params={"chat": direct_target}).json()["messages"]
                self.assertEqual({row["text"] for row in history}, {"Новое сообщение", "Старая история из файла"})

                imported = client.post("/import", headers=upload, data=form,
                                       files={"file": ("WhatsApp Chat with Команда.txt", raw)})
                self.assertEqual(imported.status_code, 200, imported.text)
                group_target = imported.json()["chat_id"]
                wait_for_collection(hub)
                self.assertEqual(len(calls), 1)
                self.assertEqual(read_state(state, AUTO_MATCHES)[group_target]["status"], "ambiguous")
                self.assertEqual(client.get(catalog, params={"owner": "admin", "target": group_target}).status_code, 403)
                self.assertEqual(len(client.get(catalog, params={"target": group_target}).json()["chats"]), 3)
                choice = {"target": group_target, "source": group["id"]}
                self.assertEqual(client.post(match, json=choice).status_code, 403)
                self.assertEqual(client.post(match, json=choice, headers=headers).status_code, 202)
                wait_for_collection(hub)
                self.assertEqual(calls[-1], (group["id"], 1581573601))
                self.assertEqual(client.get(catalog, params={"target": group_target}).status_code, 409)
                self.assertFalse((root / "archive.sqlite3").exists())
                with database(state, writable=True) as db:
                    links = get_meta(db, AUTO_LINKS)
                    self.assertGreater(links[direct_target]["next_at"], time.time())
                    links[direct_target]["next_at"] = 0
                    put_meta(db, AUTO_LINKS, links)
                messages.append({"id": "recent", "timestamp": int(time.time()) - 60, "body": "Свежая поставка"})
                hub.poll(state)
                wait_for_collection(hub)
                self.assertEqual(len(calls), 3)
                self.assertEqual(calls[-1][0], direct["id"])
                self.assertGreater(calls[-1][1], 0)
                self.assertEqual({item["chat_id"]: item["count"] for item in client.get("/api/archive").json()["chats"]},
                                 {direct_target: 3, group_target: 2})

    def test_named_export_handles_ambiguity_and_group_backfills(self):
        account = "333@c.us"
        source = {"id": "111@c.us", "name": "Склад", "kind": "direct"}
        duplicate = {"id": "113@g.us", "name": "склад", "kind": "group"}
        group = {"id": "222@g.us", "name": "Поставки", "kind": "group"}
        recent = int(time.time()) - 300
        received = []
        calls = []

        class Connector:
            def account(self):
                return account

            def messages(self, chat_id, start, through):
                calls.append((chat_id, start, through))
                yield received

        with tempfile.TemporaryDirectory() as work:
            state = Path(work)
            raw = "13.2.20, 9:00 - Анна: Подтверждаю поставку\n13.2.20, 9:02 - Олег: Принято, согласуем".encode()
            import_result = import_file(state, io.BytesIO(raw), "WhatsApp Chat with Склад.txt", name="")
            target = import_result["chat_id"]
            self.assertEqual(import_result["chat"], "Склад")
            queue_match(state, target, import_result["source_title"])
            resolve_matches(state, account, [source, duplicate])
            self.assertEqual(read_state(state, AUTO_MATCHES)[target]["status"], "ambiguous")
            self.assertFalse(calls)
            missing = import_file(state, io.BytesIO(raw), "Другое название.txt", name="Другой архив")
            queue_match(state, missing["chat_id"], missing["source_title"])
            resolve_matches(state, account, [source])
            self.assertEqual(read_state(state, AUTO_MATCHES)[missing["chat_id"]]["status"], "not_found")
            legacy = {"target": {"id": source["id"], "name": source["name"]}, "source": source,
                      "mode": "live", "floor": 0, "enabled": True, "next_at": 0}
            with database(state, writable=True) as db:
                put_meta(db, "account", account)
                put_meta(db, AUTO_LINKS, {source["id"]: legacy})
            received[:] = [{"id": "retained", "timestamp": recent, "body": "Ранее сохранённое сообщение"}]
            run_automatic(state, Connector(), [legacy], embedder=None, progress=lambda _: None)
            queue_match(state, target, import_result["source_title"])
            self.assertEqual(resolve_matches(state, account, [source]), [target])
            links = read_state(state, AUTO_LINKS)
            self.assertNotIn(source["id"], links)
            self.assertEqual(links[target]["mode"], "export")
            with database(state) as db:
                self.assertEqual(db.execute("SELECT COUNT(*) FROM messages WHERE chat_id=? AND active=1",
                                            (source["id"],)).fetchone()[0], 1)
            with self.assertRaisesRegex(ValueError, "Выберите группу"):
                save_automatic_link(state, "other", source, account, group=True)
            save_automatic_link(state, group["id"], group, account, group=True)
            received[:] = [{"id": "old", "timestamp": recent - 600000, "body": "Ранняя поставка"}]
            for _ in range(2):
                links = read_state(state, AUTO_LINKS)
                run_automatic(state, Connector(), [links[group["id"]]], embedder=None, progress=lambda _: None)
            self.assertEqual(calls[-2][1], 0)
            self.assertGreater(calls[-1][1], 0)
            self.assertEqual(read_state(state, AUTO_LINKS)[group["id"]]["mode"], "group")
            with database(state) as db:
                self.assertEqual(db.execute("SELECT COUNT(*) FROM messages WHERE chat_id=?", (group["id"],)).fetchone()[0], 1)

    def test_code_pairing_is_available_only_before_linking(self):
        current = ["SCAN_QR_CODE"]

        def handle(request):
            if request.url.path == SESSION:
                return httpx.Response(200, json={"status": current[0]})
            self.assertEqual((request.method, request.url.path), ("POST", AUTH + "/request-code"))
            self.assertEqual(request.read(), b'{"phoneNumber":"12025550123"}')
            return httpx.Response(200, json={"code": "ABCD1234"})

        with tempfile.TemporaryDirectory() as work:
            state = Path(work)
            (state / "waha.key").write_text("fixture")
            with Waha(state, httpx.MockTransport(handle)) as waha:
                self.assertEqual(waha.pairing_code("12025550123"), "ABCD-1234")
                current[0] = "WORKING"
                with self.assertRaisesRegex(ValueError, "дождитесь готовности"):
                    waha.pairing_code("12025550123")

    def test_reconnect_logs_out_existing_session_once(self):
        status = "WORKING"
        methods = []

        def handle(request):
            nonlocal status
            methods.append((request.method, request.url.path))
            if request.method == "POST":
                self.assertEqual(request.url.path, SESSION + "/logout")
                status = "SCAN_QR_CODE"
            return httpx.Response(200, json={"status": status})

        with tempfile.TemporaryDirectory() as work:
            state = Path(work)
            (state / "waha.key").write_text("fixture")
            with Waha(state, httpx.MockTransport(handle)) as waha:
                waha.reconnect()
                waha.reconnect()
        self.assertEqual([path for method, path in methods if method == "POST"], [SESSION + "/logout"])

    def test_phone_export_followed_by_incremental_updates(self):
        account = "333@c.us"
        source = {"id": "111@g.us", "name": "Склад", "kind": "group"}
        calls = []
        recent = int(time.time()) - 300

        class Connector:
            def account(self):
                return account

            def messages(self, chat_id, start, through):
                calls.append((chat_id, start))
                yield [
                    {"id": "before", "timestamp": start - 1, "body": "Уже в файле"},
                    {"id": "after", "timestamp": recent, "body": "Новое сообщение"},
                ]

        with tempfile.TemporaryDirectory() as work:
            state = Path(work)
            imported = import_file(state, io.BytesIO("1.3.16, 10:00 - Анна: Из файла".encode()),
                                   "old.txt", name="Мой архив")
            target = imported["chat_id"]
            save_automatic_link(state, target, source, account)
            links = read_state(state, AUTO_LINKS)
            self.assertEqual(links[target]["floor"], 1456815601)
            with self.assertRaisesRegex(ValueError, "уже есть привязка"):
                save_automatic_link(state, target, source, account)
            for _ in range(2):
                run_automatic(state, Connector(), links.values(), embedder=None, progress=lambda stats: None)
                links = read_state(state, AUTO_LINKS)
            self.assertEqual(calls[0], (source["id"], links[target]["floor"]))
            self.assertGreater(calls[1][1], calls[0][1])
            self.assertEqual(links[target]["last"]["saved"], 0)
            with database(state) as db:
                self.assertEqual([row[0] for row in db.execute("SELECT DISTINCT chat_id FROM messages")], [target])
                self.assertEqual(db.execute("SELECT COUNT(*) FROM messages").fetchone()[0], 2)
                self.assertEqual(lookup(db, "Новое сообщение")["matches"][0]["source"], "waha_incremental")
            hub = ArchiveHub()
            hub.pause_automatic(state, target, True)
            with self.assertRaisesRegex(ValueError, "Включите автообновление"):
                hub.update(state)
            hub.pause_automatic(state, target, False)
            self.assertTrue(read_state(state, AUTO_LINKS)[target]["enabled"])
            corrected = {"id": "222@c.us", "name": "Правильный чат", "kind": "direct"}
            save_automatic_link(state, target, corrected, account, replace=True)
            self.assertEqual(read_state(state, AUTO_LINKS)[target]["source"]["id"], corrected["id"])
            with database(state) as db:
                active = [row[0] for row in db.execute(
                    "SELECT json_extract(record, '$.source') FROM messages WHERE chat_id=? AND active=1", (target,),
                )]
                self.assertEqual(active, ["phone_export"])

    def test_selected_history_and_original(self):
        group = "111@g.us"
        private = "222@c.us"
        requested = []

        def handle(request):
            path = request.url.path
            requested.append((path, dict(request.url.params)))
            if path == SESSION:
                return httpx.Response(200, json={"status": "WORKING", "me": {"id": "333@c.us"}})
            if path == CHATS:
                return httpx.Response(200, json=[{"id": group, "name": "Склад"},
                                                 {"id": private, "name": "Личный"}])
            if path == "/api/default/groups":
                return httpx.Response(200, json={group: {"JID": group, "Name": "Склад"}})
            if path == f"{CHATS}/{group}/messages":
                return httpx.Response(200, json=[
                    {"id": "m1", "timestamp": 1490000000, "body": "Чертёж",
                     "hasMedia": True, "media": {"filename": "plan.pdf"}},
                    {"id": "m2", "timestamp": 1490000030, "body": "Подтвердили", "replyTo": {"id": "m1"}},
                ])
            if path == f"{CHATS}/{group}/messages/m1":
                return httpx.Response(200, json={"media": {
                    "url": "http://waha:3000/api/files/plan.pdf", "mimetype": "application/pdf",
                }})
            if path == "/api/files/plan.pdf":
                return httpx.Response(200, content=b"fixture PDF bytes")
            raise AssertionError(f"Unexpected request: {path}")

        with tempfile.TemporaryDirectory() as work:
            state = Path(work)
            (state / "waha.key").write_text("fixture")
            import_file(state, io.BytesIO("1.3.16, 10:00 - Анна: Договор".encode()),
                        "old.txt", name="Ручной")
            with Waha(state, httpx.MockTransport(handle)) as waha:
                account, catalog = waha.catalog()
                self.assertEqual({item["kind"] for item in catalog}, {"group", "direct"})
                self.assertFalse(any("/messages" in path for path, _ in requested))
                chosen = [item for item in catalog if item["id"] == group]
                report = import_selected(state, waha, chosen, account, full=False,
                                         embedder=None, progress=lambda stats: None)
                self.assertEqual((report["saved"], report["unavailable"]), (2, 0))
                message_paths = [params for path, params in requested if path.endswith("/messages")]
                self.assertNotIn("filter.timestamp.gte", message_paths[0])
                report = import_selected(state, waha, chosen, account, full=False,
                                         embedder=None, progress=lambda stats: None)
                self.assertEqual(report["saved"], 0)
                message_paths = [params for path, params in requested if path.endswith("/messages")]
                self.assertIn("filter.timestamp.gte", message_paths[-1])
                self.assertEqual(sum(path == "/api/files/plan.pdf" for path, _ in requested), 1)
            with database(state) as db:
                first = lookup(db, "Чертёж")["matches"][0]
                self.assertTrue(first["timestamp"].startswith("2017-"))
                self.assertEqual(first["attachments"][0]["status"], "downloaded")
                self.assertEqual(lookup(db, "Договор")["matches"][0]["source"], "phone_export")
            summary = selection_archive_summary(state)
            self.assertEqual(summary["messages"], 2)
            self.assertEqual(summary["attachments"], 1)
            self.assertTrue(summary["first"].startswith("2017-"))
            self.assertFalse(any(private in path for path, _ in requested))
