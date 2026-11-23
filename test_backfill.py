import tempfile
import unittest
from pathlib import Path

from archive import database, get_meta, identifier, put_meta, reindex, write_message
from backfill import collect_chat
from retrieval import lookup
from waha_source import utc_at


class BackfillTest(unittest.TestCase):
    def test_many_pages_resume_overlap_and_attachment(self):
        chat = {"id": "fixture@c.us", "name": "Проверочный чат"}
        start = 1795420000
        with tempfile.TemporaryDirectory() as tmp:
            state = Path(tmp)
            scratch = state / "scratch"
            scratch.mkdir()
            initial = {
                "chat_id": chat["id"], "chat": chat["name"], "message_id": "known",
                "timestamp": utc_at(start), "source": "waha", "author": "Анна",
                "from_me": False, "type": "text", "text": "Начальное сообщение",
                "caption": None, "reply_to": None, "edited": False, "deleted": False,
                "attachments": [],
            }
            with database(state, writable=True) as db:
                mid = identifier(chat["id"], initial["message_id"])
                write_message(db, mid, initial)
                put_meta(db, "live_selection", [chat])
                reindex(db, [(mid, initial)], None)
            requested = []

            def page(command):
                requested.append(command["before"]["id"])
                if len(requested) == 4:
                    return {"status": "device_start", "messages": []}
                rows = [{"id": f"m{len(requested)}-{i}", "timestamp": command["before"]["timestamp"] - i*30*86400,
                         "from_me": False, "author": "Анна", "type": "text", "reply_to": "",
                         "deleted": False, "text": "Контракт", "attachments": []} for i in range(1, 51)]
                if len(requested) == 1:
                    (scratch / "attachment-123").write_bytes(b"archive-test")
                    rows[0]["attachments"] = [{"filename": "test.pdf", "mime": "application/pdf",
                                               "size": 12, "status": "downloaded", "path": "attachment-123"}]
                return {"status": "received", "messages": rows + rows[:1]}

            first = collect_chat(state, chat, "owner", page, scratch, None, page_limit=1)
            self.assertEqual((first["status"], first["added"]), ("paused", 50))
            final = collect_chat(state, chat, "owner", page, scratch, None)
            self.assertEqual(requested, ["known", "m1-50", "m2-50", "m3-50"])
            self.assertEqual((final["status"], final["pages"], final["added"]), ("device_start", 3, 100))
            self.assertLess(final["first"], "2015-01-01")
            with database(state) as db:
                self.assertEqual(db.execute("SELECT COUNT(*) FROM messages").fetchone()[0], 151)
                self.assertEqual(get_meta(db, "backfill:" + chat["id"])["added"], 150)
                result = lookup(db, "test.pdf")["matches"][0]
                self.assertEqual(result["attachments"][0]["status"], "downloaded")
                self.assertEqual(result["source"], "peer_history")
            self.assertEqual(len(list((state / "blobs").iterdir())), 1)

    def test_timeout_retries_do_not_advance_cursor(self):
        chat = {"id": "fixture@g.us", "name": "Проверочная группа"}
        anchor = {"id": "known", "timestamp": 1795420000, "from_me": False}
        with tempfile.TemporaryDirectory() as tmp:
            state = Path(tmp)
            with database(state, writable=True) as db:
                put_meta(db, "backfill:" + chat["id"], {"cursor": anchor})
            calls, waits = [], []

            def unresponsive(command):
                calls.append(command["before"])
                return {"status": "timeout"}

            status = collect_chat(state, chat, "owner", unresponsive, state, None, wait=waits.append)
            self.assertEqual(calls, [anchor, anchor, anchor])
            self.assertEqual(waits, [5, 10])
            self.assertEqual(status["status"], "timeout")
            with database(state) as db:
                self.assertEqual(get_meta(db, "backfill:" + chat["id"])["cursor"], anchor)
