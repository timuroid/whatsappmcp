import io
import json
import re
import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest.mock import patch

from starlette.testclient import TestClient

import import_ui
from archive import database, sync
from manual_import import import_file, parse_messages
from plugin_access import AccessStore
from retrieval import history_page, lookup

TRANSCRIPT = (
    "\ufeff\u200e[16.05.2019, 08:30:19] Анна: Согласовали компрессор\n"
    "Доставка в пятницу.\n"
    "[24.11.2022, 18:42] Борис: \u200e<прикреплено: PTT.ogg>\n"
    "2/3/23, 14:10 - Анна: Заявка Z105\n"
)


class PhoneExportTest(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.state = self.root / "state"

    def test_old_history_zip_and_wamvp_updates(self):
        with io.BytesIO() as zipped:
            with zipfile.ZipFile(zipped, "w") as archive:
                archive.writestr("example/_chat.txt", TRANSCRIPT)
                archive.writestr("example/PTT.ogg", b"test voice bytes")
                archive.writestr("../independent.csv", b"a,b\n")
            data = zipped.getvalue()
        options = {"name": "История поставок"}
        first = import_file(self.state, io.BytesIO(data), "phone.zip", **options)
        self.assertEqual((first["new"], first["files"], first["unmatched"]), (3, 2, 1))
        self.assertEqual(first["first"], "2019-05-16T05:30:19Z")
        self.assertFalse((self.root / "independent.csv").exists())
        repeat = import_file(self.state, io.BytesIO(TRANSCRIPT.encode()), "phone.txt", **options)
        self.assertEqual((repeat["new"], repeat["unchanged"]), (0, 3))
        source = self.root / "empty-auto"
        source.mkdir()
        (source / "messages.jsonl").write_text("")
        (source / "status.json").write_text(json.dumps({
            "kind": "main", "schema_version": 1, "session": {"me": "fixture"},
            "totals": {"messages": 0}, "chats": [],
        }))
        self.assertEqual(sync(source, self.state)["excluded"], 0)
        with database(self.state) as db:
            result = lookup(db, "компрессор", after="2019-01-01T00:00:00Z",
                            before="2020-01-01T00:00:00Z")["matches"]
            self.assertEqual(result[0]["source"], "phone_export")
            self.assertIn("Доставка в пятницу.", result[0]["text"])
            voice = lookup(db, "PTT")["matches"][0]["attachments"][0]
            self.assertEqual(voice["status"], "downloaded")
            self.assertEqual(db.execute("SELECT count(*) FROM manual_files").fetchone()[0], 2)

    def test_reimport_links_captioned_file_without_duplicate(self):
        with io.BytesIO() as zipped:
            with zipfile.ZipFile(zipped, "w") as archive:
                archive.writestr("_chat.txt", "[16.05.2019, 08:30] Анна: Фото насоса <прикреплено: pump.jpg>")
                archive.writestr("pump.jpg", b"test image")
            data = zipped.getvalue()

        with patch("manual_import.ATTACHED", re.compile(r"(?!)")):
            old = import_file(self.state, io.BytesIO(data), "phone.zip", name="Test chat")
        self.assertEqual(old["unmatched"], 1)

        updated = import_file(self.state, io.BytesIO(data), "phone.zip", name="Test chat")
        self.assertEqual((updated["new"], updated["updated"], updated["unmatched"]), (0, 1, 0))
        with database(self.state) as db:
            messages = history_page(db, old["chat_id"])["messages"]
        self.assertEqual(len(messages), 1)
        self.assertEqual(messages[0]["text"], "Фото насоса")
        self.assertEqual(messages[0]["attachments"][0]["filename"], "pump.jpg")

    def test_upload_link_and_export_for_year(self):
        with patch.object(import_ui, "STATE", self.state), TestClient(
            import_ui.app, base_url="http://127.0.0.1:8081",
        ) as client:
            self.assertEqual(client.get("/").url.path, "/login")
            data = {"name": "Чат за 2019 год", "target": "", "timezone": "Europe/Moscow", "order": "dmy"}
            upload = [("file", ("chat.txt", TRANSCRIPT.encode(), "text/plain"))]
            self.assertEqual(client.post("/import", data=data, files=upload).status_code, 403)
            auth = {"Origin": "http://127.0.0.1:8081"}
            self.assertEqual(client.get("/api/source/qr").status_code, 401)
            key = (self.state / "admin.key").read_text().strip()
            login = client.post("/login", data={"username": "admin", "password": key}, headers=auth,
                                follow_redirects=False)
            self.assertEqual(login.status_code, 303)
            self.assertIn("HttpOnly", login.headers["Set-Cookie"])
            self.assertIn("Чаты WhatsApp", client.get("/").text)
            result = client.post("/import", data=data, files=upload, headers=auth)
            self.assertEqual(result.status_code, 200)
            self.assertIn("Чат добавлен.", result.text)
            chat = import_ui.chat_list()[0]["chat_id"]
            followup = [("file", ("history.txt", "14.08.18, 18:45 - Анна: Заказ принят".encode(), "text/plain"))]
            response = client.post("/import", data={**data, "target": chat}, files=followup, headers=auth)
            self.assertEqual(response.status_code, 200)
            self.assertEqual(len(import_ui.chat_list()), 1)
            self.assertNotIn('id="group-panel"', client.get("/").text)
            self.assertIn('id="group-panel"', client.get("/admin").text)
            dump = client.get("/export", params={"chat": chat, "year": "2019"})
            self.assertEqual(dump.status_code, 200)
            self.assertIn("whatsapp-2019.jsonl", dump.headers["Content-Disposition"])
            rows = list(map(json.loads, dump.text.splitlines()))
            self.assertEqual(len(rows), 1)
            self.assertIn("Согласовали компрессор", rows[0]["text"])
            self.assertEqual(client.get("/export?year=2017").status_code, 422)
            self.assertEqual(client.post("/admin/logout", headers=auth, follow_redirects=False).status_code, 303)
            self.assertEqual(client.get("/export").status_code, 403)
            AccessStore(self.state).change_user("create", "test.user", name="Test user", password="A test password")
            client.post("/login", data={"username": "test.user", "password": "A test password"}, headers=auth)
            self.assertEqual(client.get("/api/source/chats?owner=admin").status_code, 403)
            self.assertEqual(client.get("/api/source/qr").status_code, 404)
            self.assertEqual(client.post("/api/source/updates/group", headers=auth, json={"source": "111@g.us"}).status_code, 403)
            self.assertNotIn('id="group-panel"', client.get("/").text)

    def test_zip_upload_size_and_expansion_limits(self):
        zipped = io.BytesIO()
        with zipfile.ZipFile(zipped, "w", compression=zipfile.ZIP_DEFLATED) as bundle:
            bundle.writestr("_chat.txt", TRANSCRIPT)
            bundle.writestr("PTT.ogg", b"0" * 4096)
        payload = zipped.getvalue()
        url = "http://127.0.0.1:8081"
        with patch.object(import_ui, "STATE", self.state), TestClient(import_ui.app, base_url=url) as client:
            key = (self.state / "admin.key").read_text().strip()
            headers = {"Origin": url, "Accept": "application/json"}
            client.post("/login", data={"username": "admin", "password": key}, headers=headers)
            fields = {"name": "Тест ZIP", "timezone": "Europe/Moscow", "order": "dmy"}
            files = {"file": ("phone.zip", payload, "application/zip")}
            with patch.object(import_ui, "MAX_UPLOAD", len(payload) - 1):
                result = client.post("/import", data=fields, files=files, headers=headers)
                self.assertEqual(result.status_code, 413)
            with patch.object(import_ui, "MAX_UPLOAD", len(payload)):
                with patch("manual_import.MAX_UNPACKED", 4096):
                    result = client.post("/import", data=fields, files=files, headers=headers)
                    self.assertEqual(result.status_code, 422)
                    self.assertIn("Распакованный ZIP", result.json()["error"])
                result = client.post("/import", data=fields, files=files, headers=headers)
                self.assertEqual(result.status_code, 200)
                self.assertIn("chat_id", result.json())
            with patch.object(import_ui, "MAX_TEXT", 1):
                result = client.post("/import", data=fields, headers=headers,
                                     files={"file": ("phone.txt", TRANSCRIPT.encode(), "text/plain")})
                self.assertEqual(result.status_code, 413)

    def test_admin_history_pages_and_attachments(self):
        with io.BytesIO() as zipped:
            with zipfile.ZipFile(zipped, "w") as bundle:
                messages = "\n".join(f"15.05.19, 09:30 - Test: Смета {i}" for i in range(42))
                bundle.writestr("_chat.txt", messages + "\n[24.11.2022, 18:42] Борис: <прикреплено: PTT.ogg>\n")
                bundle.writestr("PTT.ogg", b"test voice bytes")
            chat = import_file(self.state, io.BytesIO(zipped.getvalue()), "phone.zip", name="Склад")["chat_id"]
        with database(self.state, writable=True) as db:
            db.execute("UPDATE messages SET deleted=1 WHERE chat_id=? AND json_extract(record, '$.text')='Смета 5'", (chat,))
        url = "http://127.0.0.1:8081"
        with patch.object(import_ui, "STATE", self.state), TestClient(import_ui.app, base_url=url) as client:
            key = (self.state / "admin.key").read_text().strip()
            client.post("/login", data={"username": "admin", "password": key}, headers={"Origin": url})
            first = client.get("/api/admin/history", params={"chat": chat}).json()
            older = client.get("/api/admin/history", params={"chat": chat, "before": first["next"]}).json()
            messages = first["messages"] + older["messages"]
            self.assertEqual(len({message["id"] for message in messages}), 42)
            self.assertIsNone(older["next"])
            self.assertNotIn("Смета 5", {message["text"] for message in messages})
            filtered = client.get("/api/admin/history", params={"chat": chat, "query": "смета"}).json()
            self.assertEqual(len(filtered["messages"]), 40)
            summary = client.get("/api/archive").json()["chats"]
            self.assertEqual(summary, [{"chat_id": chat, "name": "Склад", "count": 42, "first_year": 2019}])
            self.assertEqual(first, client.get("/api/archive/history", params={"chat": chat}).json())
            self.assertEqual(messages[0]["attachments"][0]["status"], "downloaded")
            path = "/api/admin/attachments/" + messages[0]["id"] + "/0"
            file = client.get(path)
            self.assertEqual(file.content, b"test voice bytes")
            self.assertEqual(file.headers["content-disposition"], 'attachment; filename="PTT.ogg"')
            client.post("/admin/logout", headers={"Origin": url})
            self.assertEqual(client.get(path).status_code, 403)

    def test_named_export_owner_applies_only_to_its_chat(self):
        data = ("15.05.19, 09:30 - Анна Петрова: Отправила\n"
                "15.05.19, 09:31 - Борис: Принял").encode()
        with self.assertRaisesRegex(ValueError, "Имя не найдено"):
            import_file(self.state, io.BytesIO(data), "chat.txt", name="Проект", owner="Другой автор")
        chat = import_file(self.state, io.BytesIO(data), "chat.txt", name="Проект",
                           owner="  анна  ПЕТРОВА  ")["chat_id"]
        repeated = import_file(self.state, io.BytesIO(data), "chat.txt", name="Проект")
        self.assertEqual(repeated["new"], 0)
        other = import_file(self.state, io.BytesIO(data), "chat.txt", name="Другой чат")["chat_id"]
        with database(self.state) as db:
            own = history_page(db, chat)["messages"]
            unrelated = history_page(db, other)["messages"]
        self.assertEqual([(item["author"], item["from_me"]) for item in own],
                         [("Борис", False), ("Анна Петрова", True)])
        self.assertFalse(any(item["from_me"] for item in unrelated))

    def test_locale_and_invalid_input(self):
        parsed = list(parse_messages("[10/31/21, 11:59:00 PM] Anna: file", "mdy", "UTC"))
        self.assertEqual(parsed[0][0], "2021-10-31T23:59:00Z")
        with self.assertRaisesRegex(ValueError, "порядок дня и месяца"):
            list(parse_messages("[10/31/21, 11:59:00 PM] Anna: file", "dmy", "UTC"))
        with self.assertRaisesRegex(ValueError, "прочитать ZIP"):
            import_file(self.state, io.BytesIO(b"not a zip"), "chat.zip", name="test")
