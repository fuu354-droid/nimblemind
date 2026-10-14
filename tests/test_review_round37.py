"""Regression tests for the thirty-seventh review round (F37-1)."""

import os

from nimblemind import migrate, recall


def test_f37_1_migrate_keeps_microseconds_so_newest_is_by_time(tmp_path, cfg):
    src = tmp_path / "old"
    src.mkdir()
    older, newer = src / "zz-older.md", src / "aa-newer.md"
    older.write_text("## Unfinished\n- OLDER journal\n", encoding="utf-8")
    newer.write_text("## Unfinished\n- NEWER journal\n", encoding="utf-8")
    base = 1_790_000_000
    os.utime(older, ns=(base * 10**9 + 100_000_000, base * 10**9 + 100_000_000))
    os.utime(newer, ns=(base * 10**9 + 900_000_000, base * 10**9 + 900_000_000))
    c = cfg()
    migrate.run(c, src, "journal")
    assert recall.latest_journal(c).source_id == "session:aa-newer"
