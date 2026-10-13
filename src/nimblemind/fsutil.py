"""File helpers that behave the same on POSIX and Windows.

Atomic replace for whole-file writes, and one advisory lock helper over
`fcntl` or `msvcrt`, so no caller has to know which platform it is on.
"""

from __future__ import annotations

import contextlib
import os
import sys
import tempfile
from pathlib import Path


def atomic_write(path: Path, text: str) -> None:
    """Write `text` to `path` so a reader sees the old file or the new one,
    never a half-written one: write a sibling temp file, then `os.replace`."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as f:
            f.write(text)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
    except BaseException:
        with contextlib.suppress(OSError):
            os.unlink(tmp)
        raise


def read_text(path: Path) -> str:
    return path.read_text(encoding="utf-8", errors="replace")


class LockBusy(Exception):
    """Raised by `file_lock(..., wait=False)` when another process holds it."""


@contextlib.contextmanager
def file_lock(path: Path, wait: bool = True):
    """Hold an exclusive advisory lock on `path` for the `with` block.

    Released when the block exits or the process dies, on either platform.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    f = open(path, "a+b")
    try:
        if sys.platform == "win32":
            import msvcrt

            f.seek(0)
            mode = msvcrt.LK_LOCK if wait else msvcrt.LK_NBLCK
            while True:
                try:
                    msvcrt.locking(f.fileno(), mode, 1)
                    break
                except OSError:
                    # LK_LOCK gives up after ~10 seconds; keep waiting when
                    # asked to, the same as flock without LOCK_NB.
                    if not wait:
                        raise LockBusy(str(path)) from None
            try:
                yield
            finally:
                f.seek(0)
                with contextlib.suppress(OSError):
                    msvcrt.locking(f.fileno(), msvcrt.LK_UNLCK, 1)
        else:
            import fcntl

            flags = fcntl.LOCK_EX if wait else fcntl.LOCK_EX | fcntl.LOCK_NB
            try:
                fcntl.flock(f.fileno(), flags)
            except BlockingIOError:
                raise LockBusy(str(path)) from None
            try:
                yield
            finally:
                fcntl.flock(f.fileno(), fcntl.LOCK_UN)
    finally:
        f.close()


def append_line(path: Path, line: str, lock: Path | None = None) -> None:
    """Append one line, under `lock` when given, so concurrent writers to one
    log never interleave a partial line."""
    path.parent.mkdir(parents=True, exist_ok=True)
    ctx = file_lock(lock) if lock else contextlib.nullcontext()
    with ctx:
        with open(path, "a", encoding="utf-8", newline="\n") as f:
            f.write(line.rstrip("\n") + "\n")


def which(name: str) -> str | None:
    """`shutil.which` without the current directory. On Windows the default
    search tries the current directory first, which is the session's
    project when a hook runs, so a `claude.cmd` planted in a repository
    would win; `NoDefaultCurrentDirectoryInExePath` turns that off, for this
    lookup and for every process started afterwards."""
    import shutil

    if sys.platform == "win32":
        os.environ["NoDefaultCurrentDirectoryInExePath"] = "1"
    return shutil.which(name)


def neutral_dir() -> str:
    """Where child processes run: never the session's project, so nothing in
    a repository is found by a relative lookup."""
    return tempfile.gettempdir()
