"""Regression tests for the eighteenth review round (F18-1)."""

import sys

import pytest

from nimblemind import model
from nimblemind.backends import BackendError
from nimblemind.backends.memila import MemilaBackend


@pytest.mark.parametrize("name", ["haiku\n", "haiku\r", "haiku\n\n"])
def test_f18_1_trailing_newline_in_a_model_name_is_refused(name, monkeypatch):
    # Never reach a real `claude` (round 19, F19-3): the lookup is faked.
    monkeypatch.setattr(model, "which", lambda n: "/nonexistent/claude")
    with pytest.raises(model.ModelError, match="cannot carry safely"):
        model.run({"command": "claude-cli", "model": name}, "s", "u")
    with pytest.raises(model.ModelError, match="cannot carry safely"):
        model.run({"command": "argv", "argv": [sys.executable, "{model}"], "model": name}, "s", "u")


@pytest.mark.parametrize("bad", ["127.0.0.1:6500", ["localhost:"], ["http://localhost:6500"], [""], [5]])
def test_malformed_forbidden_endpoints_are_refused(bad):
    with pytest.raises(BackendError):
        MemilaBackend.from_config({"name": "memila", "url": "http://127.0.0.1:1",
                                   "forbidden_endpoints": bad})


def test_wellformed_forbidden_endpoints_load():
    b = MemilaBackend.from_config({"name": "memila", "url": "http://127.0.0.1:1",
                                   "forbidden_endpoints": ["localhost:6500", ":16500", "[::1]", "10.0.0.1"]})
    assert b.forbidden == ["localhost:6500", ":16500", "[::1]", "10.0.0.1"]
