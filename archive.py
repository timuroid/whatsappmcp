"""Private archive built from the WAMVP main JSONL snapshot."""

import hashlib
import json
import os
import re
import shutil
import sqlite3
import tempfile
import unicodedata
from contextlib import contextmanager
from datetime import datetime, timezone
from functools import lru_cache
from pathlib import Path

from admin_access import ADMIN_LOGIN

STATE = Path(os.environ.get("WA_HISTORY_STATE", Path(__file__).parent / "state"))
SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS messages (
    id TEXT PRIMARY KEY, chat_id TEXT NOT NULL, timestamp TEXT NOT NULL,
    record TEXT NOT NULL, fingerprint TEXT NOT NULL,
    active INTEGER NOT NULL, deleted INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS message_time ON messages (chat_id, timestamp, id);
CREATE VIRTUAL TABLE IF NOT EXISTS terms USING fts5(id UNINDEXED, body);
CREATE TABLE IF NOT EXISTS vectors (
    id TEXT NOT NULL, chunk INTEGER NOT NULL, embedding BLOB NOT NULL,
    PRIMARY KEY(id, chunk), FOREIGN KEY(id) REFERENCES messages(id)
);
CREATE TABLE IF NOT EXISTS pending_vectors (
    id TEXT PRIMARY KEY REFERENCES messages(id),
    revision TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS transcripts (
    digest TEXT PRIMARY KEY, text TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS manual_messages (
    id TEXT PRIMARY KEY REFERENCES messages(id)
);
CREATE TABLE IF NOT EXISTS manual_files (
    chat_id TEXT NOT NULL, filename TEXT NOT NULL, digest TEXT NOT NULL,
    linked INTEGER NOT NULL,
    PRIMARY KEY(chat_id, filename, digest)
);
"""


def account_state(root, username):
    if username == ADMIN_LOGIN:
        return root
    if not isinstance(username, str) or not re.fullmatch(r"[a-z0-9][a-z0-9._-]{2,31}", username):
        raise ValueError("Invalid archive owner")
    return root / "accounts" / username


def utc(value):
    parsed = datetime.fromisoformat(value)
    if parsed.tzinfo is None:
        raise ValueError("Expected ISO timestamp with timezone")
    return parsed.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def digest(value):
    return hashlib.sha256(value.encode()).hexdigest()


def identifier(chat_id, message_id):
    return digest(json.dumps([chat_id, message_id], ensure_ascii=False))


@contextmanager
def database(state, *, writable=False):
    db_path = state / "archive.sqlite3"
    if writable:
        state.mkdir(mode=0o700, parents=True, exist_ok=True)
        db_path.touch(mode=0o600, exist_ok=True)
    db = sqlite3.connect(db_path.as_uri() + ("?mode=rw" if writable else "?mode=ro"),
                         uri=True, timeout=30)
    db.row_factory = sqlite3.Row
    try:
        if writable:
            db.execute("PRAGMA journal_mode=WAL")
            db.executescript(SCHEMA)
        else:
            db.execute("BEGIN")
        with db:
            yield db
    finally:
        db.close()


def get_meta(db, key):
    row = db.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
    return json.loads(row["value"]) if row else None


def put_meta(db, key, value):
    db.execute("INSERT OR REPLACE INTO meta VALUES (?, ?)", (key, json.dumps(value)))


def export_author_key(author):
    return " ".join(unicodedata.normalize("NFC", author or "").casefold().split())


def export_owner(db, chat_id):
    return get_meta(db, "phone_export_owner:" + chat_id) or ""


def set_export_owner(db, chat_id, author):
    put_meta(db, "phone_export_owner:" + chat_id, export_author_key(author))


def save_attachment(state, source, item):
    result = {key: item.get(key) for key in ("filename", "mime", "size", "status")}
    location = item.get("path")
    if not location or item.get("status") != "downloaded":
        return result
    media_root = (source / "media").resolve()
    file = (source / location).resolve()
    if media_root.parent != source or not file.is_relative_to(media_root):
        raise ValueError("Attachment outside the export media directory")
    expected = item.get("sha256")
    if expected and not re.fullmatch(r"[0-9a-f]{64}", expected):
        raise ValueError("Invalid attachment hash")
    if expected and (state / "blobs" / expected).is_file():
        result["digest"] = expected
        return result
    if not file.is_file():
        result["status"] = "missing_from_export"
        return result
    with file.open("rb") as origin:
        result["digest"] = save_blob(state, origin, expected)
    return result


def save_blob(state, origin, expected=None):
    blobs = state / "blobs"
    state.mkdir(mode=0o700, parents=True, exist_ok=True)
    blobs.mkdir(mode=0o700, exist_ok=True)
    with tempfile.NamedTemporaryFile(dir=blobs) as temp:
        shutil.copyfileobj(origin, temp)
        temp.flush()
        with open(temp.name, "rb") as content:
            actual = hashlib.file_digest(content, "sha256").hexdigest()
        if expected and expected != actual:
            raise ValueError("Attachment checksum mismatch")
        target = blobs / actual
        try:
            os.link(temp.name, target)
        except FileExistsError:
            pass
    return actual


def materialize(db, record):
    parts = [record.get("text") or record.get("caption") or ""]
    for attachment in record["attachments"]:
        if name := attachment.get("filename"):
            parts.append(name)
        row = db.execute("SELECT text FROM transcripts WHERE digest=?",
                         (attachment.get("digest"),)).fetchone()
        if row and row["text"]:
            parts.append(row["text"])
    return "\n".join(part for part in parts if part)


def chunks(text, size=380, overlap=60):
    start = 0
    while start < len(text):
        end = min(start + size, len(text))
        if end < len(text):
            boundary = text.rfind(" ", start + size // 2, end)
            if boundary > start:
                end = boundary
        yield text[start:end]
        if end == len(text):
            break
        start = end - overlap


def reindex(db, records, embedder):
    batch = []
    deferred = getattr(embedder, "defer_indexing", False)
    for mid, record in records:
        db.execute("DELETE FROM terms WHERE rowid=(SELECT rowid FROM messages WHERE id=?)", (mid,))
        db.execute("DELETE FROM vectors WHERE id=?", (mid,))
        if deferred:
            db.execute("DELETE FROM pending_vectors WHERE id=?", (mid,))
        if record.get("deleted"):
            continue
        body = materialize(db, record)
        if not body:
            continue
        # Link FTS rowid to the stable rowid of the canonical message.
        db.execute("INSERT INTO terms(rowid, id, body) "
                   "SELECT rowid, id, ? FROM messages WHERE id=?", (body, mid))
        if deferred:
            db.execute("INSERT INTO pending_vectors VALUES (?, lower(hex(randomblob(16))))", (mid,))
        elif embedder:
            batch.extend((mid, i, chunk) for i, chunk in enumerate(chunks(body)))
    if batch:
        encoded = embedder.encode([entry[2] for entry in batch])
        db.executemany("INSERT INTO vectors VALUES (?, ?, ?)",
                      ((mid, i, vector.astype("<f4").tobytes())
                       for (mid, i, _), vector in zip(batch, encoded, strict=True)))


def prepare_index(db, embedder):
    model = embedder.identity if embedder else None
    existing_model = get_meta(db, "model")
    if existing_model is not None and existing_model != model:
        raise ValueError("Для этого архива нужна та же модель поиска, что использовалась при создании.")
    if model is not None and existing_model is None:
        if getattr(embedder, "defer_indexing", False):
            queue_existing_vectors(db)
        else:
            rows = db.execute("SELECT id, record FROM messages")
            while batch := rows.fetchmany(32):
                reindex(db, [(row["id"], json.loads(row["record"])) for row in batch], embedder)
    put_meta(db, "model", model)


def queue_existing_vectors(db):
    db.execute("""INSERT OR IGNORE INTO pending_vectors
                  SELECT id, lower(hex(randomblob(16))) FROM messages WHERE deleted=0""")


def rebuild_embedding_index(state, embedder):
    if not (state / "archive.sqlite3").is_file():
        return 0
    with database(state, writable=True) as db:
        db.execute("BEGIN IMMEDIATE")
        if get_meta(db, "model") != embedder.identity:
            db.execute("DELETE FROM vectors")
            db.execute("DELETE FROM pending_vectors")
            put_meta(db, "model", None)
            prepare_index(db, embedder)
        return db.execute("""SELECT COUNT(*) FROM pending_vectors JOIN messages USING(id)
                             WHERE active=1 AND deleted=0""").fetchone()[0]


def index_pending_vectors(state, embedder):
    if not (state / "archive.sqlite3").is_file():
        return 0
    with database(state) as db:
        if get_meta(db, "model") != embedder.identity:
            return 0
        rows = db.execute("""SELECT p.id, p.revision, m.record
                             FROM pending_vectors p JOIN messages m USING(id)
                             WHERE m.active=1 AND m.deleted=0
                             ORDER BY p.rowid LIMIT 32""").fetchall()
        texts = []
        batches = []
        for row in rows:
            start = len(texts)
            texts.extend(chunks(materialize(db, json.loads(row["record"]))))
            batches.append((row["id"], row["revision"], start, len(texts)))
    if not batches:
        return 0
    vectors = embedder.encode(texts) if texts else []
    with database(state, writable=True) as db:
        db.execute("BEGIN IMMEDIATE")
        if get_meta(db, "model") != embedder.identity:
            return 0
        for mid, revision, start, end in batches:
            if not db.execute("DELETE FROM pending_vectors WHERE id=? AND revision=?", (mid, revision)).rowcount:
                continue
            db.executemany("INSERT INTO vectors VALUES (?, ?, ?)",
                           ((mid, i, vector.astype("<f4").tobytes())
                            for i, vector in enumerate(vectors[start:end])))
    return len(batches)


def write_message(db, mid, record):
    serialized = json.dumps(record, ensure_ascii=False, sort_keys=True)
    checksum = digest(serialized)
    previous = db.execute("SELECT fingerprint FROM messages WHERE id=?", (mid,)).fetchone()
    if previous and previous["fingerprint"] == checksum:
        return False
    db.execute(
        """INSERT INTO messages VALUES (?, ?, ?, ?, ?, 1, ?)
           ON CONFLICT(id) DO UPDATE SET timestamp=excluded.timestamp,
               record=excluded.record, fingerprint=excluded.fingerprint, deleted=excluded.deleted""",
        (mid, record["chat_id"], record["timestamp"], serialized, checksum, bool(record.get("deleted"))),
    )
    return True


def sync(source, state, embedder=None, *, chat_ids=None):
    source = source.resolve()
    state = state.resolve()
    if source == state or state.is_relative_to(source):
        raise ValueError("Store the index outside the WAMVP export directory")
    snapshot = source / "messages.jsonl"
    status_path = source / "status.json"
    status_bytes = status_path.read_bytes()
    published_at = status_path.stat().st_mtime_ns
    status = json.loads(status_bytes)
    if status.get("kind") != "main" or status.get("schema_version") != 1:
        raise ValueError("Expected a WAMVP main export, schema version 1")
    session = status.get("session") or {}
    if not session.get("me"):
        raise ValueError("The export does not identify a WhatsApp account")
    selection = set(chat_ids) if chat_ids is not None else None
    expected = {chat["chat_id"]: chat["messages"] for chat in status["chats"]}
    if len(expected) != len(status["chats"]) or sum(expected.values()) != status["totals"]["messages"]:
        raise ValueError("Inconsistent export counts")
    counts = {}
    stats = {"indexed": 0, "unchanged": 0, "excluded": 0}
    with database(state, writable=True) as db, snapshot.open(encoding="utf-8") as reader:
        original = os.fstat(reader.fileno())
        if original.st_mtime_ns > published_at:
            raise ValueError("Snapshot publication is in progress; retry")
        db.execute("BEGIN IMMEDIATE")
        if get_meta(db, "account") not in (None, session["me"]):
            raise ValueError("Use a separate index directory for another WhatsApp account")
        put_meta(db, "account", session["me"])
        prepare_index(db, embedder)
        db.execute("CREATE TEMP TABLE seen (id TEXT PRIMARY KEY)")
        pending = []
        for line in reader:
            record = json.loads(line)
            chat_id = record["chat_id"]
            counts[chat_id] = counts.get(chat_id, 0) + 1
            if selection is not None and chat_id not in selection:
                continue
            mid = identifier(chat_id, record["message_id"])
            db.execute("INSERT INTO seen VALUES (?)", (mid,))
            record["timestamp"] = utc(record["timestamp"])
            # Exclude upstream phone-number fields from the derived archive.
            record = {key: record.get(key) for key in (
                "chat_id", "message_id", "chat", "timestamp", "author", "from_me",
                "type", "text", "caption", "reply_to", "edited", "deleted", "attachments",
            )}
            record["attachments"] = [
                save_attachment(state, source, item) for item in record["attachments"] or []
            ]
            if not write_message(db, mid, record):
                stats["unchanged"] += 1
                continue
            pending.append((mid, record))
            stats["indexed"] += 1
            if len(pending) >= 32:
                reindex(db, pending, embedder)
                pending.clear()
        current = snapshot.stat()
        if (counts != expected or status_path.stat().st_mtime_ns != published_at
                or status_path.read_bytes() != status_bytes
                or (current.st_ino, current.st_mtime_ns) != (original.st_ino, original.st_mtime_ns)):
            raise ValueError("Export changed during import or snapshot counts differ; retry")
        reindex(db, pending, embedder)
        stats["excluded"] = db.execute(
            "UPDATE messages SET active=0 WHERE active=1 "
            "AND id NOT IN (SELECT id FROM seen) AND id NOT IN (SELECT id FROM manual_messages) "
            "AND COALESCE(json_extract(record, '$.source'), 'wamvp') NOT IN ('waha', 'peer_history')"
        ).rowcount
        db.execute("UPDATE messages SET active=1 WHERE active=0 AND id IN (SELECT id FROM seen)")
        put_meta(db, "sync_at", status.get("generated_at"))
        put_meta(db, "source_sync_ok", status.get("last_sync_ok"))
        put_meta(db, "requested_from", status.get("requested_from"))
    return stats


@lru_cache(maxsize=1)
def whisper_model(model_path):
    from faster_whisper import WhisperModel

    return WhisperModel(str(model_path), device="cpu", compute_type="int8", local_files_only=True)


def transcribe(state, model_path, embedder=None):
    with database(state, writable=True) as db:
        existing = get_meta(db, "model")
        if existing != (embedder.identity if embedder else None):
            raise ValueError("Use the same embedding model as in the existing index")
        pending = db.execute("""
            SELECT DISTINCT json_extract(a.value, '$.digest') AS digest
            FROM messages m, json_each(m.record, '$.attachments') a
            WHERE m.active AND NOT m.deleted
              AND (json_extract(a.value, '$.mime') LIKE 'audio/%'
                   OR json_extract(m.record, '$.type') IN ('voice', 'audio'))
              AND digest IS NOT NULL AND digest NOT IN (SELECT digest FROM transcripts)
        """).fetchall()
    done = failed = 0
    if not pending:
        return {"transcribed": done, "retry": failed}
    model = whisper_model(model_path)
    for row in pending:
        checksum = row["digest"]
        try:
            segments, _ = model.transcribe(str(state / "blobs" / checksum), beam_size=3)
            text = " ".join(segment.text.strip() for segment in segments)
        except (ValueError, OSError, RuntimeError, EOFError):
            failed += 1
            continue
        with database(state, writable=True) as db:
            db.execute("BEGIN IMMEDIATE")
            db.execute("INSERT OR IGNORE INTO transcripts VALUES (?, ?)", (checksum, text))
            affected = db.execute("""
                SELECT id, record FROM messages
                WHERE EXISTS (SELECT 1 FROM json_each(record, '$.attachments')
                              WHERE json_extract(value, '$.digest')=?)
            """, (checksum,)).fetchall()
            for message in affected:
                reindex(db, [(message["id"], json.loads(message["record"]))], embedder)
        done += 1
    return {"transcribed": done, "retry": failed}
