"""Index a WAMVP export once or keep importing completed snapshots."""

import argparse
import json
import sqlite3
import sys
import time
from pathlib import Path

from archive import STATE, rebuild_embedding_index, sync, transcribe
from retrieval import configured_embedder


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("export_dir", type=Path, nargs="?",
                        help="WAMVP export directory; omit to transcribe already imported audio")
    parser.add_argument("--watch", type=int, metavar="SECONDS",
                        help="poll for a new snapshot at this interval")
    parser.add_argument("--chat-list", type=Path,
                        help="optional text file with one allowed chat ID per line")
    parser.add_argument("--whisper-model", type=Path, help="local faster-whisper model directory")
    parser.add_argument("--reindex-embeddings", action="store_true",
                        help="rebuild the index in WA_HISTORY_STATE with the configured embedding model")
    args = parser.parse_args()
    if args.watch is not None and args.watch < 10:
        parser.error("--watch must be at least 10 seconds")
    if args.whisper_model:
        args.whisper_model = args.whisper_model.expanduser().resolve(strict=True)
    state = STATE.expanduser().resolve()
    embedder = configured_embedder()
    if args.reindex_embeddings:
        if not embedder or args.export_dir or args.watch or args.chat_list or args.whisper_model:
            parser.error("--reindex-embeddings requires an embedding model and runs on its own")
        pending = rebuild_embedding_index(state, embedder)
        print(json.dumps({"pending_embeddings": pending}))
        return 0
    if args.export_dir is None:
        if not args.whisper_model or args.watch or args.chat_list:
            parser.error("specify the export directory or use --whisper-model by itself")
        result = transcribe(state, args.whisper_model, embedder)
        print(json.dumps(result))
        return int(bool(result["retry"]))
    source = args.export_dir.expanduser().resolve()
    last = None
    while True:
        try:
            selected = args.chat_list.read_text().split() if args.chat_list else None
            snapshot = (source / "status.json").stat()
            revision = (snapshot.st_ino, snapshot.st_mtime_ns, selected)
            if revision != last:
                print(json.dumps(sync(source, state, embedder, chat_ids=selected)), flush=True)
                if args.whisper_model:
                    result = transcribe(state, args.whisper_model, embedder)
                    print(json.dumps(result), flush=True)
                    if not result["retry"]:
                        last = revision
                else:
                    last = revision
        except (OSError, ValueError, KeyError, TypeError, sqlite3.Error) as exc:
            print(f"Import did not complete ({type(exc).__name__}). Check snapshot, account and model.",
                  file=sys.stderr, flush=True)
            if args.watch is None:
                return 1
        if args.watch is None:
            return 0
        time.sleep(args.watch)


if __name__ == "__main__":
    raise SystemExit(main())
