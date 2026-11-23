"""Import a WhatsApp phone export as a searchable chat."""

import json
import mimetypes
import re
import unicodedata
import zipfile
from collections import Counter
from datetime import datetime
from pathlib import Path, PurePosixPath
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from archive import (
    database, digest, export_author_key, identifier, prepare_index, reindex, save_blob, set_export_owner, utc,
    write_message,
)

MAX_UPLOAD = 512 * 1024 * 1024
MAX_TEXT = 64 * 1024 * 1024
MAX_UNPACKED = 1024 * 1024 * 1024
MAX_ENTRIES = 15000
DATE = r"\d{1,4}[./-]\d{1,2}[./-]\d{1,4}"
TIME = r"\d{1,2}:\d{2}(?::\d{2})?(?:\s*[AP]\.?\s*M\.?)?"
STAMP = rf"(?P<day>{DATE})[,\s]+(?P<clock>{TIME})"
HEADER = re.compile(rf"^(?:\[{STAMP}\]\s*|{STAMP.replace('?P<', '?P<bare_')}\s+[-–]\s+)(?P<body>.*)$",
                    re.IGNORECASE)
MARKS = str.maketrans("", "", "\u200e\u200f\u202a\u202b\u202c\u202d\u202e\u2066\u2067\u2068\u2069")
ATTACHED = re.compile(
    r"^(?:(?P<caption>.*)\s)?<(?:attached|прикреплено):\s*(?P<marked_file>.+)>$"
    r"|^(?P<bare_file>.+?)\s+\((?:file attached|файл прикрепл[её]н)\)$",
    re.IGNORECASE,
)
TRANSCRIPT_NAMES = {"_chat.txt", "chat.txt"}
EXPORT_PREFIX = re.compile(
    r"^(?:whatsapp\s+chat\s+with|чат\s+whatsapp\s+с)\s+|"
    r"^(?:whatsapp(?:\s+chat)?|чат\s+whatsapp|whatsapp\s+чат)\s*[-–—_:]\s*",
    re.IGNORECASE,
)


def export_title(filename):
    stem = PurePosixPath(filename.replace("\\", "/")).stem.translate(MARKS)
    title = EXPORT_PREFIX.sub("", unicodedata.normalize("NFC", stem)).strip()
    generic = {"chat", "_chat", "whatsapp", "whatsapp chat", "чат", "чат whatsapp", "переписка"}
    return title if 1 <= len(title) <= 120 and title.casefold() not in generic else None


def archive_timestamp(day, clock, order, tz):
    parts = re.split(r"[./-]", day)
    if len(parts[0]) == 4:
        year, month, date = map(int, parts)
    else:
        a, b, year = map(int, parts)
        date, month = (a, b) if order == "dmy" else (b, a)
        if len(parts[-1]) == 2:
            year += 2000
    match = re.fullmatch(r"(\d{1,2}):(\d{2})(?::(\d{2}))?\s*(AM|PM)?",
                         re.sub(r"[.\s]", "", clock.upper()))
    if not match:
        raise ValueError("Некорректное время")
    hours, minutes, seconds, meridiem = match.groups()
    hours = int(hours)
    if meridiem:
        if not 1 <= hours <= 12:
            raise ValueError("Некорректное время AM/PM")
        hours = hours % 12 + (12 if meridiem == "PM" else 0)
    return utc(datetime(year, month, date, hours, int(minutes), int(seconds or 0), tzinfo=tz).isoformat())


def parse_messages(text, order, timezone):
    if order not in ("dmy", "mdy"):
        raise ValueError("Выберите порядок дня и месяца.")
    try:
        tz = ZoneInfo(timezone)
    except (ValueError, ZoneInfoNotFoundError) as exc:
        raise ValueError("Укажите часовой пояс, например Europe/Moscow.") from exc
    timestamp = None
    body = []
    for line_number, line in enumerate(text.translate(MARKS).splitlines(), 1):
        match = HEADER.fullmatch(line)
        if not match:
            if timestamp is not None:
                body.append(line)
            continue
        if timestamp is not None:
            yield parse_body(timestamp, body)
        try:
            timestamp = archive_timestamp(match["day"] or match["bare_day"],
                                          match["clock"] or match["bare_clock"], order, tz)
        except ValueError as exc:
            raise ValueError(f"Не удалось прочитать дату в строке {line_number}. Проверьте порядок дня и месяца.") from exc
        body = [match["body"]]
    if timestamp is None:
        raise ValueError("В TXT не найдено датированных сообщений WhatsApp.")
    yield parse_body(timestamp, body)


def parse_body(timestamp, lines):
    author, separator, first = lines[0].partition(": ")
    if not separator or not author or len(author) > 128:
        author, first = None, lines[0]
    text = "\n".join([first, *lines[1:]]).strip()
    match = ATTACHED.fullmatch(first.strip())
    legacy_text = None
    filename = None
    if match:
        caption = match["caption"]
        if caption:
            legacy_text = text
        filename = PurePosixPath((match["marked_file"] or match["bare_file"]).replace("\\", "/")).name
        text = "\n".join([caption or "", *lines[1:]]).strip()
    return timestamp, author, text, filename, legacy_text


def decode_text(reader):
    data = reader.read(MAX_TEXT + 1)
    if len(data) > MAX_TEXT:
        raise ValueError("Текст чата превышает 64 МБ.")
    try:
        return data.decode("utf-8-sig")
    except UnicodeDecodeError as exc:
        raise ValueError("Ожидается исходный TXT WhatsApp в UTF-8.") from exc


def zip_contents(archive):
    entries = [entry for entry in archive.infolist() if not entry.is_dir()
               and not entry.filename.startswith("__MACOSX/")
               and PurePosixPath(entry.filename).name != ".DS_Store"]
    if len(entries) > MAX_ENTRIES or sum(entry.file_size for entry in entries) > MAX_UNPACKED:
        raise ValueError("ZIP слишком большой. Лимит распакованных файлов — 1 ГБ.")
    text_entries = [entry for entry in entries if PurePosixPath(entry.filename).suffix.casefold() == ".txt"]
    chat = [entry for entry in text_entries if PurePosixPath(entry.filename).name.casefold() in TRANSCRIPT_NAMES]
    if not chat:
        chat = text_entries
    if len(chat) != 1:
        raise ValueError("В ZIP должен быть один текст переписки. Загрузите исходный архив одного чата.")
    return chat[0], [entry for entry in entries if entry is not chat[0]]


def save_media(archive, entries, state):
    items = []
    for entry in entries:
        filename = PurePosixPath(entry.filename.replace("\\", "/")).name
        if not filename:
            continue
        with archive.open(entry) as reader:
            checksum = save_blob(state, reader)
        items.append({"filename": filename, "mime": mimetypes.guess_type(filename)[0],
                      "size": entry.file_size, "digest": checksum, "status": "downloaded"})
    return items


def import_file(state, reader, filename, *, name, target=None, order="dmy",
                timezone="Europe/Moscow", embedder=None, require_new=False, owner=""):
    name = unicodedata.normalize("NFC", name.strip())
    source_title = export_title(filename)
    state = state.resolve()
    suffix = Path(filename).suffix.lower()
    try:
        if suffix == ".txt":
            messages = list(parse_messages(decode_text(reader), order, timezone))
            media = []
        elif suffix == ".zip":
            with zipfile.ZipFile(reader) as archive:
                transcript, entries = zip_contents(archive)
                source_title = source_title or export_title(transcript.filename)
                with archive.open(transcript) as content:
                    messages = list(parse_messages(decode_text(content), order, timezone))
                media = save_media(archive, entries, state)
        else:
            raise ValueError("Выберите TXT или ZIP из WhatsApp.")
    except (zipfile.BadZipFile, NotImplementedError, RuntimeError) as exc:
        raise ValueError("Не удалось прочитать ZIP. Выберите исходный архив без пароля.") from exc
    name = name or source_title or ""
    if not target and not 1 <= len(name) <= 120:
        raise ValueError("Введите название чата.")
    owner = owner.strip()
    owner_key = export_author_key(owner)
    if owner and (len(owner) > 128 or not any(
        export_author_key(author) == owner_key for _, author, _, _, _ in messages
    )):
        raise ValueError("Имя не найдено среди авторов. Укажите его так, как оно записано в файле.")

    names = Counter(item["filename"] for item in media)
    attachments = {item["filename"]: item for item in media if names[item["filename"]] == 1}
    used = set()
    seen = Counter()
    stats = {"new": 0, "updated": 0, "unchanged": 0, "files": len(media)}
    with database(state, writable=True) as db:
        db.execute("BEGIN IMMEDIATE")
        if target:
            rows = db.execute("""SELECT chat_id, json_extract(record, '$.chat') AS name FROM messages
                                 WHERE chat_id=? ORDER BY active DESC, timestamp DESC LIMIT 1""",
                              (target,)).fetchone()
            if not rows:
                raise ValueError("Этот чат отсутствует в архиве. Обновите страницу.")
            chat_id, name = rows["chat_id"], rows["name"] or "Чат без названия"
        else:
            chat_id = "manual:" + digest(name.casefold())
            if require_new and db.execute("SELECT 1 FROM messages WHERE chat_id=? LIMIT 1", (chat_id,)).fetchone():
                raise ValueError("Такой чат уже есть. Выберите «В существующий» или измените название.")
        if owner:
            set_export_owner(db, chat_id, owner)
        prepare_index(db, embedder)
        batch = []
        for timestamp, author, text, original, legacy_text in messages:
            # Earlier imports included the attachment marker in the caption text.
            key_fields = (legacy_text, None) if legacy_text is not None else (text, original)
            key = json.dumps([timestamp, author, *key_fields], ensure_ascii=False)
            seen[key] += 1
            external_id = "export:" + digest(key + f":{seen[key]}")
            mid = identifier(chat_id, external_id)
            attachment = attachments.get(original)
            if attachment:
                used.add((attachment["filename"], attachment["digest"]))
            prior = db.execute("SELECT record FROM messages WHERE id=?", (mid,)).fetchone()
            if prior and not attachment:
                # An earlier ZIP can provide media absent from the later text export.
                prior_items = json.loads(prior["record"])["attachments"]
            else:
                prior_items = []
            items = [attachment] if attachment else prior_items
            if original and not items:
                items = [{"filename": original, "status": "missing_from_export"}]
            record = {"chat_id": chat_id, "chat": name, "message_id": external_id,
                      "timestamp": timestamp, "author": author, "text": text, "attachments": items,
                      "source": "phone_export", "from_me": False, "caption": None, "reply_to": None,
                      "edited": False, "deleted": False, "type": "audio" if items and
                      (items[0].get("mime") or "").startswith("audio/") else "text"}
            if write_message(db, mid, record):
                db.execute("INSERT OR IGNORE INTO manual_messages VALUES (?)", (mid,))
                batch.append((mid, record))
                stats["updated" if prior else "new"] += 1
                if len(batch) == 32:
                    reindex(db, batch, embedder)
                    batch.clear()
            else:
                stats["unchanged"] += 1
        reindex(db, batch, embedder)
        for item in media:
            key = (item["filename"], item["digest"])
            db.execute("""INSERT INTO manual_files VALUES (?, ?, ?, ?)
                          ON CONFLICT(chat_id, filename, digest) DO UPDATE SET linked=max(linked, excluded.linked)""",
                       (chat_id, *key, int(key in used)))
    return {**stats, "chat": name, "chat_id": chat_id, "source_title": source_title, "unmatched": len(media) - sum(
        (item["filename"], item["digest"]) in used for item in media
    ), "first": min(row[0] for row in messages), "last": max(row[0] for row in messages)}
