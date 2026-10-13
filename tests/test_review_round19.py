"""Regression tests for the nineteenth review round (F19-1..F19-3)."""

import json
import sys

import pytest

from nimblemind import fsutil, journal, model, writes
from nimblemind.backends import BackendError
from nimblemind.backends.memila import MemilaBackend, is_forbidden


def load(entries):
    return MemilaBackend.from_config({"name": "memila", "url": "http://127.0.0.1:1",
                                      "forbidden_endpoints": entries})


@pytest.mark.parametrize("bad", ["localhost: 6452", "localhost :6452", "localhost:abc",
                                 "localhost:+6452", "localhost:0x1934", "[::1]:x", "[::1]x",
                                 "*", "*.corp", "user@localhost:6452", "http:localhost",
                                 "local host", "localhost#x", "localhost:99999", "localhost:0",
                                 " localhost", "[127.0.0.1]", "[::1"])
def test_f19_1_malformed_entries_are_config_errors(bad):
    with pytest.raises(BackendError):
        load([bad])


@pytest.mark.parametrize("good", ["localhost", "localhost:6452", ":6452", "127.0.0.1",
                                  "127.0.0.1:6500", "[::1]", "[::1]:6500", "::1",
                                  "rag-api.internal", "rag-api.internal.:443"])
def test_f19_1_wellformed_entries_load(good):
    assert load([good]).forbidden == [good]


def test_f19_1_unreadable_entry_counts_as_forbidden():
    assert is_forbidden("http://127.0.0.1:18977/x", ["127.0.0.1: 18977"])


def test_f19_2_namespace_with_a_line_separator_audits_clean(cfg):
    c = cfg()
    for ns in ("team\u2028a", "c\x85d", "e\u2029f"):
        r = writes.write(c, "cli", "note", "hello", namespace=ns, approved_by="t")
        assert r.state == writes.STORED
        assert writes.audit(c, ns) == []
    assert writes.audit(c) == []


def test_f19_2_journal_sections_ignore_unicode_line_separators(cfg):
    text = "## Unfinished\n- a\u2028## Pitfalls fake\n- b"
    assert journal.section(text, "Unfinished") == "- a\u2028## Pitfalls fake\n- b"


def test_f19_3_name_is_checked_before_looking_for_claude(monkeypatch):
    looked = []
    monkeypatch.setattr(model, "which", lambda n: looked.append(n) or None)
    with pytest.raises(model.ModelError, match="cannot carry safely"):
        model.run({"command": "claude-cli", "model": "haiku\n"}, "s", "u")
    assert looked == []


def test_float_distiller_timeout_from_the_environment(tmp_path, monkeypatch):
    from nimblemind import config as config_mod
    monkeypatch.setenv("NIMBLEMIND_DISTILLER_TIMEOUT", "2.5")
    assert config_mod.load(tmp_path)["distiller"]["timeout"] == 2.5


@pytest.mark.skipif(sys.platform != "win32", reason="Windows drive semantics")
def test_rooted_path_without_a_drive_is_not_absolute_on_windows():
    assert not fsutil.is_absolute("\\x")
    assert fsutil.is_absolute("C:\\x")
