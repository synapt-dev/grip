"""An OSError creating or writing `.gr2-slot` after `store init` has succeeded is one stderr line, not a traceback.

The pointer is advisory; the store is already on disk when it is written. A failed open or write
must not turn a completed init into exit 1 (the exit table has no 1), and must not leave a partial
pointer that every later init would keep, because an existing file is never touched.
"""
from __future__ import annotations

import errno
import os
from pathlib import Path

import pytest

from tests.test_store_init_slot_pointer import _init, root  # noqa: F401  (fixture)

_orig_open = os.open
_orig_fdopen = os.fdopen


def _enospc(path: object = "") -> OSError:
    return OSError(errno.ENOSPC, "No space left on device", str(path))


class _FullFile:
    """A file on a full device: the entry exists, every write fails."""

    def __init__(self, fd: int) -> None:
        self._f = _orig_fdopen(fd, "w")

    def __enter__(self) -> "_FullFile":
        return self

    def __exit__(self, *exc: object) -> bool:
        self._f.close()
        return False

    def write(self, text: str) -> int:
        raise _enospc()


def _fail_before_open(monkeypatch: pytest.MonkeyPatch) -> None:
    def fake_open(path, flags, *a, **k):
        if os.path.basename(os.fspath(path)) == ".gr2-slot":
            raise _enospc(path)
        return _orig_open(path, flags, *a, **k)

    monkeypatch.setattr(os, "open", fake_open)


def _fail_after_create(monkeypatch: pytest.MonkeyPatch) -> None:
    slot_fds: set[int] = set()

    def fake_open(path, flags, *a, **k):
        fd = _orig_open(path, flags, *a, **k)
        if os.path.basename(os.fspath(path)) == ".gr2-slot":
            slot_fds.add(fd)
        return fd

    def fake_fdopen(fd, *a, **k):
        if fd in slot_fds:
            return _FullFile(fd)
        return _orig_fdopen(fd, *a, **k)

    monkeypatch.setattr(os, "open", fake_open)
    monkeypatch.setattr(os, "fdopen", fake_fdopen)


@pytest.mark.parametrize("failing", [_fail_before_open, _fail_after_create], ids=["before-open", "after-create"])
def test_slot_write_oserror_is_one_line_and_leaves_no_pointer(root: Path, monkeypatch, failing) -> None:  # noqa: F811
    failing(monkeypatch)
    code, err = _init(root, {"GR2_SLOT": "dev-x"})
    assert code == 0, err
    assert (root / "grip.toml").exists()
    assert not (root / ".gr2-slot").exists()
    lines = [line for line in err.splitlines() if "GR2_SLOT" in line]
    assert len(lines) == 1, err
    assert "Traceback" not in err
