from __future__ import annotations

import fcntl
import json
import os
from datetime import UTC, datetime
from pathlib import Path


def _now_utc() -> str:
    return datetime.now(UTC).isoformat()


def _events_dir(workspace_root: Path) -> Path:
    return workspace_root / ".grip" / "events"


def _outbox_file(workspace_root: Path) -> Path:
    return _events_dir(workspace_root) / "outbox.jsonl"


def _outbox_lock_file(workspace_root: Path) -> Path:
    return _events_dir(workspace_root) / "outbox.lock"


def append_outbox_event(workspace_root: Path, payload: dict[str, object]) -> None:
    outbox_path = _outbox_file(workspace_root)
    lock_path = _outbox_lock_file(workspace_root)
    outbox_path.parent.mkdir(parents=True, exist_ok=True)
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    try:
        with lock_path.open("a+", encoding="utf-8") as lock_fh:
            fcntl.flock(lock_fh.fileno(), fcntl.LOCK_EX)
            seq = 1
            if outbox_path.exists():
                with outbox_path.open("r", encoding="utf-8") as existing:
                    for line in existing:
                        line = line.strip()
                        if not line:
                            continue
                        try:
                            row = json.loads(line)
                        except json.JSONDecodeError:
                            continue
                        value = int(row.get("seq", 0))
                        if value >= seq:
                            seq = value + 1
            event = {
                "version": 1,
                "seq": seq,
                "event_id": os.urandom(8).hex(),
                "timestamp": _now_utc(),
                **payload,
            }
            with outbox_path.open("a", encoding="utf-8") as fh:
                fh.write(json.dumps(event) + "\n")
            fcntl.flock(lock_fh.fileno(), fcntl.LOCK_UN)
    except OSError:
        return
