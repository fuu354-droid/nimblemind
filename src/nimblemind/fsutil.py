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
    """The absolute path of an executable, or None. Never the current
    directory: on Windows the default search tries it first (it is the
    session's project when a hook runs), which `NoDefaultCurrentDirectory
    InExePath` turns off for this lookup and every child started afterwards.
    A result that is not absolute (a PATH entry of "" or ".") is refused too,
    since it would be resolved again in whatever directory the child runs."""
    import shutil

    if sys.platform == "win32":
        os.environ["NoDefaultCurrentDirectoryInExePath"] = "1"
    found = shutil.which(name)
    return found if found and is_absolute(found) else None


def run_dir() -> str:
    """Where a detached child runs: a directory under the data home, private
    to this user. Not the session's project (a repository's files would be
    found by relative lookups) and not the shared system temp directory
    (another local user could plant files, or a `.claude/` and `CLAUDE.md`
    a child `claude` would read)."""
    from nimblemind.paths import data_home

    d = data_home() / "run"
    d.mkdir(parents=True, exist_ok=True)
    try:
        os.chmod(d, 0o700)
    except OSError:
        pass
    return str(d)


@contextlib.contextmanager
def private_dir():
    """A fresh empty directory under `run_dir()` for one child process,
    removed afterwards."""
    import shutil

    d = tempfile.mkdtemp(prefix="child-", dir=run_dir())
    try:
        yield d
    finally:
        shutil.rmtree(d, ignore_errors=True)


def logical_cwd() -> str:
    """The current directory as the shell names it, symlinks unresolved.
    `os.getcwd()` returns the physical path on POSIX, with every symlink
    already followed, so `$PWD` is used whenever it is absolute and names
    the same directory; otherwise `getcwd()`."""
    pwd = os.environ.get("PWD", "")
    here = os.getcwd()
    if pwd and os.path.isabs(pwd):
        try:
            if os.path.samefile(pwd, here):
                return os.path.normpath(pwd)
        except OSError:
            pass
    return here


def is_absolute(path: str) -> bool:
    """Absolute on every platform: on Windows that also needs a drive or a
    UNC share, since `\\x` (rooted, no drive) is relative to the current
    drive and `os.path.isabs` calls it absolute before Python 3.13."""
    if not os.path.isabs(path):
        return False
    if sys.platform == "win32":
        drive, _ = os.path.splitdrive(path)
        return bool(drive)
    return True
