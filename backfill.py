"""Resume backfill of the selected chats through an authorized whatsmeow device."""

import argparse
import base64
import fcntl
import json
import os
import re
import subprocess
import tempfile
import time
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path

from archive import STATE, database, get_meta, identifier, prepare_index, put_meta, reindex, save_blob, write_message
from retrieval import configured_embedder
from waha_source import utc_at

TERMINAL = {"device_start", "device_limit", "session_unavailable", "account_mismatch", "invalid_anchor"}
RETRYABLE = {"timeout", "request_failed", "no_progress"}


def cursor(row):
    return {"id": row["id"], "timestamp": int(row["timestamp"]), "from_me": bool(row["from_me"])}


def initial_cursor(db, chat):
    row = db.execute("""
        SELECT record FROM messages
        WHERE chat_id=? AND active=1 AND deleted=0 AND id NOT IN (SELECT id FROM manual_messages)
        ORDER BY timestamp, id LIMIT 1
    """, (chat,)).fetchone()
    if row is None:
        return None
    record = json.loads(row["record"])
    return {"id": record["message_id"], "from_me": record["from_me"],
            "timestamp": int(datetime.fromisoformat(record["timestamp"]).timestamp())}


def record_attachment(state, scratch, item):
    entry = {key: item.get(key) for key in ("filename", "mime", "size", "status")}
    if entry["status"] == "downloaded":
        name = item.get("path", "")
        if not re.fullmatch(r"attachment-\d+", name):
            raise ValueError("Некорректный путь временного вложения.")
        with (scratch / name).open("rb") as media:
            entry["digest"] = save_blob(state, media)
    return entry


def store_batch(state, chat, page, checkpoint, embedder, scratch):
    before = checkpoint["cursor"]
    at_boundary = set(checkpoint.get("boundary", [])) | {before["id"]}
    older = []
    seen = set()
    for row in page.get("messages") or []:
        pos = cursor(row)
        if not pos["id"] or not 0 < pos["timestamp"] <= before["timestamp"] or pos["id"] in seen:
            continue
        if pos["timestamp"] == before["timestamp"] and pos["id"] in at_boundary:
            continue
        older.append(row)
        seen.add(pos["id"])
    if not older:
        return checkpoint, 0
    earliest = min(older, key=lambda row: row["timestamp"])
    advance = cursor(earliest)
    boundary = (at_boundary if advance["timestamp"] == before["timestamp"] else set())
    boundary |= {row["id"] for row in older if row["timestamp"] == advance["timestamp"]}

    with database(state, writable=True) as db:
        db.execute("BEGIN IMMEDIATE")
        selected = {item["id"] for item in get_meta(db, "live_selection") or []}
        if chat["id"] not in selected:
            raise ValueError("Чат больше не выбран для синхронизации.")
        changes = []
        new = 0
        for item in older:
            mid = identifier(chat["id"], item["id"])
            old = db.execute("SELECT record, deleted FROM messages WHERE id=?", (mid,)).fetchone()
            previous = json.loads(old["record"]) if old else None
            attachments = [record_attachment(state, scratch, entry) for entry in item["attachments"]]
            if previous:
                downloaded = [entry for entry in previous["attachments"] if entry["status"] == "downloaded"]
                if downloaded:
                    attachments = downloaded
                if old["deleted"] or not any(entry["status"] == "downloaded" for entry in attachments):
                    continue
                # Keep the current text and update only the recovered attachments.
                record = {**previous, "attachments": attachments}
            else:
                record = {"chat_id": chat["id"], "chat": chat["name"], "message_id": item["id"],
                          "timestamp": utc_at(item["timestamp"]), "author": item["author"] or None,
                          "from_me": bool(item["from_me"]), "type": item["type"], "text": item["text"],
                          "caption": None, "reply_to": item["reply_to"] or None, "edited": False,
                          "deleted": bool(item["deleted"]), "attachments": attachments,
                          "source": "peer_history"}
            if write_message(db, mid, record):
                changes.append((mid, record))
                new += previous is None
        reindex(db, changes, embedder)
        done = {"cursor": advance, "boundary": sorted(boundary),
                "pages": checkpoint.get("pages", 0) + 1, "added": checkpoint.get("added", 0) + new}
        put_meta(db, "backfill:" + chat["id"], done)
    return done, new


def result_for(checkpoint, reason, total):
    return {"status": reason, "first": utc_at(checkpoint["cursor"]["timestamp"]) if checkpoint else None,
            "pages": checkpoint.get("pages", 0) if checkpoint else 0, "added": total}


def collect_chat(state, chat, account, request, scratch, embedder, *, page_limit=None,
                 attempts=3, wait=time.sleep, report=lambda value: None):
    key = "backfill:" + chat["id"]
    with database(state, writable=True) as db:
        db.execute("BEGIN IMMEDIATE")
        prepare_index(db, embedder)
        checkpoint = get_meta(db, key)
        if not checkpoint:
            first = initial_cursor(db, chat["id"])
            if not first:
                return result_for(None, "no_anchor", 0)
            checkpoint = {"cursor": first}

    processed = failures = total = 0
    while page_limit is None or processed < page_limit:
        before = checkpoint["cursor"]
        report(result_for(checkpoint, "requesting", total))
        page = request({"account": account, "chat": chat["id"], "before": before})
        reason = page["status"]
        if reason == "received" or reason in ("device_start", "device_limit"):
            saved, added = store_batch(state, chat, page, checkpoint, embedder, scratch)
            # The worker owns these temporary files and no longer needs them after a batch.
            for temp in scratch.glob("attachment-*"):
                temp.unlink()
            if reason in TERMINAL:
                return result_for(saved, reason, total + added)
            if saved != checkpoint:
                checkpoint = saved
                processed += 1
                failures = 0
                total += added
                continue
            reason = "no_progress"
        if reason not in RETRYABLE:
            return result_for(checkpoint, reason, total)
        failures += 1
        if failures >= attempts:
            return result_for(checkpoint, reason, total)
        wait(5 * 2 ** (failures - 1))
    return result_for(checkpoint, "paused", total)


@contextmanager
def private_worker(state, binary, store):
    with (state / "backfill.lock").open("a") as lock, tempfile.TemporaryDirectory(
        prefix="peer-", dir=state,
    ) as temporary:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise ValueError("Догрузка уже запущена.") from exc
        scratch = Path(temporary)
        proc = subprocess.Popen([str(binary), "-store", str(store), "-media", temporary],
                                stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                stderr=subprocess.DEVNULL, text=True, encoding="utf-8")
        try:
            def call(payload):
                proc.stdin.write(json.dumps(payload) + "\n")
                proc.stdin.flush()
                line = proc.stdout.readline()
                if not line:
                    raise ValueError("Процесс догрузки завершился до получения ответа.")
                return json.loads(line)

            yield call, scratch
        finally:
            proc.stdin.close()
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                proc.terminate()
                proc.wait(timeout=5)
            proc.stdout.close()


def backfill(state, store, binary, embedder, *, name=None, page_limit=None,
             attempts=3, progress=lambda value: None):
    with database(state) as db:
        account = get_meta(db, "account")
        selected = get_meta(db, "live_selection") or []
    if name:
        selected = [chat for chat in selected if chat["name"].casefold() == name.casefold()]
        if len(selected) != 1:
            raise ValueError("Имя должно однозначно совпадать с выбранным чатом.")
    if not account or not selected:
        raise ValueError("Сначала выгрузите выбранные чаты через WAHA.")
    results = []
    with private_worker(state, binary, store) as (request, scratch):
        for index, chat in enumerate(selected, 1):
            def report(result):
                progress({**result, "current": index, "total": len(selected)})

            result = collect_chat(state, chat, account, request, scratch, embedder,
                                  page_limit=page_limit, attempts=attempts, report=report)
            report(result)
            results.append(result)
            if result["status"] in ("session_unavailable", "account_mismatch"):
                break
    with database(state, writable=True) as db:
        put_meta(db, "backfill_result", results)
    return results


def pair_device(state, store, binary, show_qr):
    with database(state) as db:
        account = get_meta(db, "account")
    if not account:
        raise ValueError("Сначала выгрузите выбранные чаты через WAHA.")
    store.touch(mode=0o600, exist_ok=True)
    with subprocess.Popen([str(binary), "-pair", "-store", str(store)], stdin=subprocess.PIPE,
                          stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True) as proc:
        proc.stdin.write(json.dumps({"account": account}) + "\n")
        proc.stdin.close()
        for line in proc.stdout:
            response = json.loads(line)
            state = response["status"]
            if state == "qr":
                show_qr(base64.b64decode(response["png"], validate=True))
            elif state == "paired":
                return
            elif state == "account_mismatch":
                raise ValueError("Для догрузки нужен тот же WhatsApp-аккаунт, что выбран в архиве.")
        raise ValueError("Не удалось привязать устройство для догрузки. Попробуйте снова.")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--device-store", type=Path, required=True)
    parser.add_argument("--worker", type=Path)
    parser.add_argument("--chat", help="exact name from the saved selection")
    parser.add_argument("--pages", type=int, help="optional per-chat limit for a diagnostic run")
    parser.add_argument("--attempts", type=int, default=3)
    args = parser.parse_args()
    if (args.pages is not None and args.pages < 1) or not 1 <= args.attempts <= 5:
        parser.error("pages must be positive; attempts must be in 1–5")
    state = STATE.expanduser().resolve()
    worker = args.worker or state / "wa-peer"
    results = backfill(state, args.device_store.resolve(strict=True), worker.resolve(strict=True),
                       configured_embedder(), name=args.chat, page_limit=args.pages, attempts=args.attempts)
    print(json.dumps(results, ensure_ascii=False))


if __name__ == "__main__":
    main()
