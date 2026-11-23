"""Hybrid retrieval with scoped source evidence for the MCP server."""

import json
import os
import re
from collections import defaultdict
from functools import lru_cache
from pathlib import Path

import httpx

from archive import export_author_key, export_owner, get_meta, identifier, utc


class EmbeddingUnavailable(RuntimeError):
    pass


class OpenAIEmbedder:
    model = "text-embedding-3-small"
    dimensions = 512
    identity = f"openai:{model}:{dimensions}"
    defer_indexing = True

    def __init__(self, api_key, *, transport=None):
        if not api_key:
            raise ValueError("Для OpenAI Embeddings задайте OPENAI_API_KEY в окружении сервера.")
        self.http = httpx.Client(
            base_url="https://api.openai.com/v1/",
            headers={"Authorization": f"Bearer {api_key}"}, timeout=12, transport=transport,
        )

    def encode(self, texts):
        import numpy as np

        vectors = []
        for start in range(0, len(texts), 64):
            batch = texts[start:start + 64]
            try:
                response = self.http.post("embeddings", json={
                    "model": self.model, "dimensions": self.dimensions, "input": batch,
                })
                response.raise_for_status()
                rows = sorted(response.json()["data"], key=lambda row: row["index"])
                if [row["index"] for row in rows] != list(range(len(batch))):
                    raise ValueError("Unexpected embedding indexes")
                encoded = np.asarray([row["embedding"] for row in rows], dtype=np.float32)
                if encoded.shape != (len(batch), self.dimensions) or not np.isfinite(encoded).all():
                    raise ValueError("Unexpected embedding shape")
            except (httpx.HTTPError, KeyError, TypeError, ValueError) as exc:
                raise EmbeddingUnavailable("Не удалось получить эмбеддинги OpenAI.") from exc
            vectors.extend(encoded)
        return vectors


class Embedder:
    def __init__(self, path):
        from sentence_transformers import SentenceTransformer

        path = Path(path).expanduser().resolve(strict=True)
        self.identity = str(path)
        self.model = SentenceTransformer(str(path), device="cpu", local_files_only=True,
                                         trust_remote_code=False)

    def encode(self, texts):
        return self.model.encode(texts, normalize_embeddings=True, show_progress_bar=False)


@lru_cache(maxsize=1)
def configured_embedder():
    if os.environ.get("WA_HISTORY_EMBED_PROVIDER") == "openai":
        return OpenAIEmbedder(os.environ.get("OPENAI_API_KEY", "").strip())
    model = os.environ.get("WA_HISTORY_EMBED_MODEL")
    return Embedder(model) if model else None


def cite(db, row, query="", *, full=False, phone_owner=""):
    record = json.loads(row["record"])
    text = record.get("text") or record.get("caption") or ""
    source = record.get("source", "wamvp")
    outgoing = bool(record["from_me"]) or (
        source == "phone_export" and bool(phone_owner) and export_author_key(record["author"]) == phone_owner
    )
    attachments = []
    for item in record["attachments"] if full else record["attachments"][:3]:
        entry = {key: value for key, value in item.items() if key != "digest"}
        transcript = db.execute("SELECT text FROM transcripts WHERE digest=?",
                                (item.get("digest"),)).fetchone()
        if transcript:
            entry["transcript_excerpt"] = transcript["text"] if full else excerpt(transcript["text"], query, 500)
        attachments.append(entry)
    return {
        "id": row["id"],
        "chat": record["chat"] or "Чат без названия",
        "author": record["author"] or ("Владелец аккаунта" if outgoing else None),
        **({"from_me": outgoing} if full else {}),
        "timestamp": record["timestamp"],
        "source": source,
        "text": text if full else excerpt(text, query, 1200),
        "edited": bool(record["edited"]),
        "reply_to_id": identifier(row["chat_id"], record["reply_to"]) if record["reply_to"] else None,
        "attachments": attachments,
    }


def terms(query):
    return re.findall(r"[^\W_]+", query.casefold())[:30]


def excerpt(text, query, limit):
    if len(text) <= limit:
        return text
    folded = text.casefold()
    positions = [offset for word in terms(query) if (offset := folded.find(word)) >= 0]
    start = min(max(0, min(positions, default=0) - 100), len(text) - limit)
    return ("…" if start else "") + text[start:start + limit] + ("…" if start + limit < len(text) else "")


def scope(chat, after, before):
    where = ["m.active=1", "m.deleted=0"]
    args = []
    if chat:
        # SQLite lower() only handles ASCII, so apply Unicode casefold in Python.
        where.append("instr(casefold(COALESCE(json_extract(m.record, '$.chat'), '')), ?) > 0")
        args.append(chat.casefold())
    if after:
        where.append("m.timestamp >= ?")
        args.append(utc(after))
    if before:
        where.append("m.timestamp < ?")
        args.append(utc(before))
    return " AND ".join(where), args


def vector_hits(db, query, embedder, where, args, size):
    import numpy as np

    rows = db.execute("SELECT m.id, v.embedding FROM vectors v JOIN messages m USING(id) WHERE " + where, args)
    batch = rows.fetchmany(512)
    if not batch:
        return None
    vector = embedder.encode([query])[0]
    best = {}
    while batch:
        matrix = np.stack([np.frombuffer(row["embedding"], dtype="<f4") for row in batch])
        for row, similarity in zip(batch, matrix @ vector, strict=True):
            if similarity >= 0.38:
                mid = row["id"]
                best[mid] = max(float(similarity), best.get(mid, 0))
        batch = rows.fetchmany(512)
    return [mid for mid, _ in sorted(best.items(), key=lambda result: (-result[1], result[0]))[:size]]


def lookup(db, query, *, chat=None, after=None, before=None, limit=8, embedder=None):
    query = query.strip()
    if not query or len(query) > 900 or not 1 <= limit <= 15:
        raise ValueError("Expected a query of 1–900 characters and a limit of 1–15")
    if after and before and utc(after) >= utc(before):
        raise ValueError("The end of the date interval must be after its start")
    model = get_meta(db, "model")
    if embedder and model not in (None, embedder.identity):
        raise ValueError("Use the same embedding model as in the existing index")
    db.create_function("casefold", 1, str.casefold, deterministic=True)
    where, args = scope(chat, after, before)
    words = list(dict.fromkeys(terms(query)))
    candidates = []
    pool = limit * 4
    if words:
        match = " OR ".join(f'"{word}"' for word in words)
        rows = db.execute(
            "SELECT m.id FROM terms JOIN messages m ON terms.rowid=m.rowid "
            "WHERE terms MATCH ? AND " + where + " ORDER BY bm25(terms), m.id LIMIT ?",
            [match, *args, pool],
        )
        candidates.append([row["id"] for row in rows])
    semantic = None
    if model and embedder:
        try:
            semantic = vector_hits(db, query, embedder, where, args, pool)
        except EmbeddingUnavailable:
            pass
        if semantic is not None:
            candidates.append(semantic)
    scores = defaultdict(float)
    for matches in candidates:
        for rank, mid in enumerate(matches, 1):
            scores[mid] += 1 / (60 + rank)
    ordered = sorted(scores, key=lambda mid: (-scores[mid], mid))[:limit]
    return {
        "mode": "hybrid" if semantic is not None else "keyword",
        "snapshot_at": get_meta(db, "sync_at"),
        "matches": [
            cite(db, db.execute("SELECT * FROM messages WHERE id=?", (mid,)).fetchone(), query)
            for mid in ordered
        ],
    }


def context(db, message_id, neighbors):
    if not re.fullmatch(r"[0-9a-f]{64}", message_id) or not 0 <= neighbors <= 5:
        raise ValueError("Expected a source ID from search and 0–5 neighbors")
    center = db.execute("SELECT * FROM messages WHERE id=? AND active=1 AND deleted=0",
                        (message_id,)).fetchone()
    if not center:
        return {"message": None, "before": [], "after": []}
    args = (center["chat_id"], center["timestamp"], center["id"], neighbors)
    base = "SELECT * FROM messages WHERE active=1 AND deleted=0 AND chat_id=?"
    before = db.execute(base + " AND (timestamp,id)<(?,?) ORDER BY timestamp DESC,id DESC LIMIT ?", args)
    after = db.execute(base + " AND (timestamp,id)>(?,?) ORDER BY timestamp,id LIMIT ?", args)
    return {
        "message": cite(db, center),
        "before": [cite(db, row) for row in reversed(before.fetchall())],
        "after": [cite(db, row) for row in after],
    }


def history_page(db, chat_id, *, before="", query="", size=40):
    filters, args = scope(None, None, None)
    filters += " AND m.chat_id=?"
    args.append(chat_id)
    source = "messages m"
    if before:
        cursor = db.execute("SELECT timestamp FROM messages WHERE id=? AND chat_id=?", (before, chat_id)).fetchone()
        if not cursor:
            raise ValueError("Этот участок истории изменился. Откройте чат заново.")
        filters += " AND (m.timestamp,m.id)<(?,?)"
        args.extend((cursor["timestamp"], before))
    if query:
        words = list(dict.fromkeys(terms(query)))
        if not words:
            return {"messages": [], "next": None}
        source = "terms JOIN messages m ON terms.rowid=m.rowid"
        filters += " AND terms MATCH ?"
        args.append(" OR ".join(f'"{word}"' for word in words))
    rows = db.execute(
        f"SELECT m.* FROM {source} WHERE {filters} ORDER BY m.timestamp DESC,m.id DESC LIMIT ?",
        [*args, size + 1],
    ).fetchall()
    phone_owner = export_owner(db, chat_id)
    return {"messages": [cite(db, row, full=True, phone_owner=phone_owner) for row in rows[:size]],
            "next": rows[size - 1]["id"] if len(rows) > size else None}


def coverage(db):
    return {
        "snapshot_at": get_meta(db, "sync_at"),
        "source_sync_ok": get_meta(db, "source_sync_ok"),
        "requested_from": get_meta(db, "requested_from"),
        "history_limit": "Only messages present in the WhatsApp export. Missing records do not prove absence.",
        "chats": [dict(row) for row in db.execute("""
            SELECT json_extract(record, '$.chat') AS chat, MIN(timestamp) AS first_message_at,
                   MAX(timestamp) AS last_message_at, COUNT(*) AS messages
            FROM messages WHERE active=1 AND deleted=0 GROUP BY chat_id
            ORDER BY chat LIMIT 100
        """)],
    }
