import os
import sys
from pathlib import Path

import pytest

from nimblemind import config as config_mod


def toml_list(items) -> str:
    """A TOML array of literal strings: no escaping, so Windows paths survive."""
    return "[" + ", ".join(f"'{i}'" for i in items) + "]"


@pytest.fixture(autouse=True)
def isolated_env(tmp_path, monkeypatch):
    """Every test gets its own HOME, data home and user config, and no
    memila settings from the machine running the suite."""
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("USERPROFILE", str(home))
    monkeypatch.setenv("NIMBLEMIND_HOME", str(tmp_path / "data"))
    monkeypatch.setenv("NIMBLEMIND_CONFIG", str(tmp_path / "user.toml"))
    for k in list(os.environ):
        if k.startswith(("MEMILA_URL", "MEMILA_API_KEY", "NIMBLEMIND_CHILD")):
            monkeypatch.delenv(k, raising=False)
        if k.startswith("NIMBLEMIND_") and k not in ("NIMBLEMIND_HOME", "NIMBLEMIND_CONFIG"):
            monkeypatch.delenv(k, raising=False)
    from nimblemind.backends import memila

    monkeypatch.setattr(memila, "ENV_FILE", home / ".config" / "memila.env")
    yield


@pytest.fixture
def user_config(tmp_path):
    def write(text: str) -> Path:
        p = tmp_path / "user.toml"
        p.write_text(text, encoding="utf-8")
        return p
    return write


@pytest.fixture
def cfg(tmp_path):
    def load(cwd=None):
        return config_mod.load(cwd or tmp_path)
    return load


@pytest.fixture
def fake_model(tmp_path):
    """An argv model command: a python script that prints a fixed reply."""
    def make(reply: str) -> list[str]:
        script = tmp_path / "fake_model.py"
        out = tmp_path / "fake_reply.txt"
        out.write_text(reply, encoding="utf-8")
        script.write_text(
            "import sys\nsys.stdin.read()\n"
            f"sys.stdout.write(open({str(out)!r}, encoding='utf-8').read())\n",
            encoding="utf-8")
        return [sys.executable, str(script)]
    return make
