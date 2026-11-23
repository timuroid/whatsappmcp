import asyncio
import hashlib
import json
import os
import subprocess
import sys
import tempfile
import unittest
from collections import Counter
from importlib.util import find_spec
from pathlib import Path
from unittest.mock import patch

import httpx
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client
from starlette.testclient import TestClient

from archive import database, identifier, index_pending_vectors, rebuild_embedding_index, sync, transcribe
from retrieval import EmbeddingUnavailable, OpenAIEmbedder, configured_embedder, context, lookup


class ArchiveTest(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.source = Path(temp.name) / "export"
        self.source.mkdir()
        self.state = Path(temp.name) / "archive"
        self.embedder = configured_embedder()

    def make_attachment(self, mid, name, mime, data):
        path = self.source / "media" / mid / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
        return dict(path=str(path.relative_to(self.source)), filename=name, mime=mime,
                    size=len(data), sha256=hashlib.sha256(data).hexdigest(), status="downloaded")

    def publish(self, *rows, account="synthetic", expected=None):
        data = "".join(json.dumps(row, ensure_ascii=False) + "\n" for row in rows)
        counts = Counter(row["chat_id"] for row in rows)
        (self.source / "messages.jsonl").write_text(data, encoding="utf-8")
        (self.source / "status.json").write_text(json.dumps(dict(
            kind="main", schema_version=1, session={"me": account}, totals={"messages": expected or len(rows)},
            generated_at="2026-11-23T11:00:00Z", last_sync_ok=True, requested_from="2025-06-01",
            chats=[{"chat_id": chat_id, "messages": count} for chat_id, count in counts.items()],
        )), encoding="utf-8")

    def row(self, mid, text, *, chat="supply", minute=0, **extra):
        return dict(chat_id=chat, message_id=mid, chat="Снабжение" if chat == "supply" else "Личное",
                    timestamp=f"2026-11-23T10:{minute:02d}:00Z", author="Анна", text=text,
                    attachments=[], **{k: v for k, v in extra.items() if k != "attachments"}) | {
                        "attachments": extra.get("attachments", []),
                    }

    @unittest.skipUnless(find_spec("numpy"), "OpenAI Embeddings requires NumPy")
    def test_openai_indexing_is_deferred_and_keyword_search_survives_outage(self):
        failed = True
        requests = []

        def respond(request):
            self.assertEqual(str(request.url), "https://api.openai.com/v1/embeddings")
            self.assertEqual(request.headers["Authorization"], "Bearer fixture-key")
            payload = json.loads(request.content)
            self.assertEqual((payload["model"], payload["dimensions"]), ("text-embedding-3-small", 512))
            requests.append(payload["input"])
            if failed:
                return httpx.Response(429)
            return httpx.Response(200, json={"data": [
                {"index": index, "embedding": [1.0] + [0.0] * 511}
                for index in reversed(range(len(payload["input"])))
            ]})

        first = self.row("m1", "Доставка компрессора в пятницу.")
        self.publish(first)
        sync(self.source, self.state)
        embedder = OpenAIEmbedder("fixture-key", transport=httpx.MockTransport(respond))
        self.addCleanup(embedder.http.close)
        self.assertEqual(rebuild_embedding_index(self.state, embedder), 1)
        self.publish(first, self.row("m2", "Подтвердили отгрузку.", minute=1))
        sync(self.source, self.state, embedder)
        self.assertEqual(requests, [])
        with self.assertRaises(EmbeddingUnavailable):
            index_pending_vectors(self.state, embedder)
        with database(self.state) as db:
            self.assertEqual(db.execute("SELECT count(*) FROM pending_vectors").fetchone()[0], 2)
            self.assertEqual(lookup(db, "компрессора", embedder=embedder)["mode"], "keyword")
        failed = False
        self.assertEqual(index_pending_vectors(self.state, embedder), 2)
        self.assertEqual(set(requests[-1]), {"Доставка компрессора в пятницу.", "Подтвердили отгрузку."})
        with database(self.state) as db:
            self.assertEqual(db.execute("SELECT DISTINCT length(embedding) FROM vectors").fetchone()[0], 2048)
            result = lookup(db, "Когда привезут технику?", embedder=embedder)
            self.assertEqual(result["mode"], "hybrid")
            self.assertEqual(len(result["matches"]), 2)
            failed = True
            result = lookup(db, "компрессора", embedder=embedder)
            self.assertEqual(result["mode"], "keyword")
            self.assertEqual(len(result["matches"]), 1)
        self.assertEqual(rebuild_embedding_index(self.state, embedder), 0)

    def test_incremental_scope_edits_and_mcp(self):
        data = b"item,count\ncompressor,3\n"
        attachment = self.make_attachment("m1", "order-841.csv", "text/csv", data)
        first = self.row("m1", "Привезём оборудование в четверг.", attachments=[attachment])
        private = self.row("m2", "Пароль для теста", chat="personal")
        self.publish(first, private)
        self.assertEqual(sync(self.source, self.state, self.embedder, chat_ids=["supply"])["indexed"], 1)
        self.assertEqual(sync(self.source, self.state, self.embedder, chat_ids=["supply"])["unchanged"], 1)
        self.assertEqual((self.state / "blobs" / attachment["sha256"]).read_bytes(), data)
        mid = identifier("supply", "m1")
        with database(self.state) as db:
            result = lookup(db, "841", chat="СНАБЖЕНИЕ", embedder=self.embedder)
            self.assertEqual(result["matches"][0]["id"], mid)
            if self.embedder:
                search = lookup(db, "Когда доставят технику?", embedder=self.embedder)
                self.assertEqual(search["mode"], "hybrid")
                self.assertEqual(search["matches"][0]["id"], mid)
            self.assertFalse(lookup(db, "пароль")["matches"])
        reply = self.row("m3", "Подтверждаю пятницу.", reply_to="m1", minute=1)
        edited = {**first, "text": "Привезём оборудование в пятницу.", "edited": True}
        self.publish(edited, private, reply)
        self.assertEqual(sync(self.source, self.state, self.embedder, chat_ids=["supply"])["indexed"], 2)
        with database(self.state) as db:
            self.assertEqual(context(db, mid, 2)["after"][0]["reply_to_id"], mid)
            self.assertEqual(len(lookup(db, "пятницу", after="2026-11-23T10:01:00Z")["matches"]), 1)
            self.assertFalse(lookup(db, "четверг")["matches"])
        self.publish(private, expected=2)
        with self.assertRaises(ValueError):
            sync(self.source, self.state, self.embedder)
        with database(self.state) as db:
            self.assertIsNotNone(context(db, mid, 2)["message"])
        asyncio.run(self.check_mcp(mid))
        self.check_http(mid)
        self.publish(private)
        self.assertEqual(sync(self.source, self.state, self.embedder, chat_ids=[])["excluded"], 2)
        with database(self.state) as db:
            self.assertFalse(lookup(db, "пятницу", embedder=self.embedder)["matches"])
            self.assertIsNone(context(db, mid, 2)["message"])
        self.publish(edited | {"deleted": True}, reply)
        sync(self.source, self.state, self.embedder)
        with database(self.state) as db:
            self.assertIsNone(context(db, mid, 2)["message"])
            self.assertEqual(len(lookup(db, "пятницу")["matches"]), 1)

    async def check_mcp(self, mid):
        server = StdioServerParameters(
            command=sys.executable, args=[str(Path(__file__).with_name("mcp_server.py"))],
            env={**os.environ, "WA_HISTORY_STATE": str(self.state)},
        )
        async with stdio_client(server) as (read, write), ClientSession(read, write) as client:
            await client.initialize()
            tools = (await client.list_tools()).tools
            self.assertEqual({tool.name for tool in tools}, {"search_messages", "get_context", "archive_status"})
            self.assertTrue(all(tool.annotations.readOnlyHint for tool in tools))
            result = await client.call_tool("get_context", {"message_id": mid})
            self.assertFalse(result.isError, result.content)
            message = result.structuredContent["message"]
            self.assertEqual(message["text"], "Привезём оборудование в пятницу.")
            self.assertNotIn(mid, result.model_dump_json())
            search = await client.call_tool("search_messages", {"query": "841", "chat": "Снабжение"})
            self.assertFalse(search.isError, search.content)
            self.assertNotIn(mid, search.model_dump_json())
            first = search.structuredContent["matches"][0]
            self.assertNotIn("id", first)
            location = {field: first[field] for field in ("chat", "timestamp", "author")}
            result = await client.call_tool("get_context", location)
            self.assertFalse(result.isError, result.content)
            self.assertEqual(result.structuredContent["message"], message)
            reply = result.structuredContent["after"][0]
            self.assertEqual(reply["reply_to"], location)
            self.assertNotIn("reply_to_id", reply)
            self.assertNotIn(mid, result.model_dump_json())
            result = await client.call_tool("archive_status", {})
            self.assertEqual(len(result.structuredContent["chats"]), 1)

    def check_http(self, mid):
        import mcp_server

        with patch.object(mcp_server, "STATE", self.state), TestClient(
            mcp_server.app.streamable_http_app(), base_url="http://127.0.0.1:8079",
            headers={"Accept": "application/json, text/event-stream"},
        ) as client:
            response = client.post("/mcp", json={
                "jsonrpc": "2.0", "id": 1, "method": "tools/call",
                "params": {"name": "get_context", "arguments": {"message_id": mid}},
            })
            self.assertEqual(response.status_code, 200, response.text)
            message = response.json()["result"]["structuredContent"]["message"]
            self.assertEqual(message["chat"], "Снабжение")
            self.assertNotIn("id", message)
            self.assertNotIn(mid, response.text)

    def test_context_by_source_does_not_guess_between_messages(self):
        self.publish(self.row("m1", "Первое"), self.row("m2", "Второе"))
        sync(self.source, self.state)
        with database(self.state) as db:
            result = context(db, None, 2, chat="Снабжение", timestamp="2026-11-23T10:00:00Z", author="Анна")
        self.assertTrue(result["ambiguous"])
        self.assertIsNone(result["message"])

    @unittest.skipUnless(os.environ.get("TEST_WHISPER_MODEL"), "local model not specified")
    def test_speech_is_searchable(self):
        source = self.source / "voice.aiff"
        subprocess.run(["say", "-v", "Samantha", "-o", str(source),
                        "The compressor arrives on Friday. Check the compressor before delivery."], check=True)
        attachment = self.make_attachment("audio", "memo.aiff", "audio/aiff", source.read_bytes())
        self.publish(self.row("audio", "", type="voice", attachments=[attachment]))
        sync(self.source, self.state, self.embedder)
        model = Path(os.environ["TEST_WHISPER_MODEL"])
        self.assertEqual(transcribe(self.state, model, self.embedder)["transcribed"], 1)
        with database(self.state) as db:
            match = lookup(db, "compressor")["matches"][0]
            self.assertIn("friday", match["attachments"][0]["transcript_excerpt"].lower())
        self.assertEqual(transcribe(self.state, model, self.embedder)["transcribed"], 0)


if __name__ == "__main__":
    unittest.main()
