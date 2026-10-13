"""The memila backend: a self-hosted memila instance over HTTP.

Optional and never the default. Configured by `[backend]` keys, then
`MEMILA_URL` / `MEMILA_API_KEY`, then `~/.config/memila.env`, then
`http://localhost:6452`.

`forbidden_endpoints` lists `host:port` (or `:port` for any host) pairs a
request must never reach. It is compared by resolved address, not by text,
because `127.1` and `2130706433` both reach 127.0.0.1 and a list of
spellings is a list of holes. The port is tested first, so a lookup only
happens when the port could match.
"""

from __future__ import annotations

import ipaddress
import json
import os
import socket
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

from memila_brain.backends import BackendError
from memila_brain.records import Evidence, Record

DEFAULT_URL = "http://localhost:6452"
ENV_FILE = Path.home() / ".config" / "memila.env"


def _env_file() -> dict[str, str]:
    try:
        lines = ENV_FILE.read_text(encoding="utf-8").splitlines()
    except OSError:
        return {}
    pairs = (ln.split("=", 1) for ln in lines if "=" in ln and not ln.lstrip().startswith("#"))
    return {k.strip(): v.strip() for k, v in pairs}


def _addresses(host: str) -> set[str]:
    try:
        return {ipaddress.ip_address(info[4][0]).compressed
                for info in socket.getaddrinfo(host, None)}
    except (OSError, ValueError):
        return set()


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """Refuse every redirect. A redirect would reach a host the forbidden
    check never saw, carrying the API key with it; memila never redirects,
    so one is treated as a failure, not followed."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise urllib.error.HTTPError(req.full_url, code,
                                     f"redirect to {newurl} refused", headers, fp)


# No proxy from the environment either: a proxy variable would route the
# request, and the API key, through a host the forbidden check never saw.
_OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}), _NoRedirect)


def is_forbidden(url: str, forbidden: list[str]) -> bool:
    parts = urllib.parse.urlsplit(url)
    try:
        port = parts.port or (443 if parts.scheme == "https" else 80)
    except ValueError:
        return True  # a port that does not parse cannot be shown to be allowed
    host = parts.hostname or ""
    for entry in forbidden:
        f_host, _, f_port = entry.rpartition(":")
        f_host = f_host.strip("[]")  # "[::1]:6500" names the host ::1
        if not f_port.isdigit() or int(f_port) != port:
            continue
        if not f_host:
            return True
        if _addresses(host) & _addresses(f_host):
            return True
    return False


class MemilaBackend:
    name = "memila"

    def __init__(self, url: str, api_key: str = "", timeout: float = 30,
                 forbidden_endpoints: list[str] | None = None):
        self.url = url.rstrip("/")
        self.api_key = api_key
        self.timeout = timeout
        self.forbidden = list(forbidden_endpoints or [])

    @classmethod
    def from_config(cls, spec: dict) -> "MemilaBackend":
        env = _env_file()
        url = spec.get("url") or os.environ.get("MEMILA_URL") or env.get("MEMILA_URL") or DEFAULT_URL
        key = os.environ.get("MEMILA_API_KEY") or env.get("MEMILA_API_KEY") or ""
        return cls(url, key, float(spec.get("timeout", 30)), spec.get("forbidden_endpoints"))

    def _call(self, path: str, body: dict, method: str = "POST", timeout: float | None = None):
        if is_forbidden(self.url, self.forbidden):
            raise BackendError(f"refused: {self.url} is a forbidden endpoint")
        headers = {"Content-Type": "application/json"}
        if self.api_key:
            headers["X-Api-Key"] = self.api_key
        req = urllib.request.Request(self.url + path, data=json.dumps(body).encode(),
                                     headers=headers, method=method)
        try:
            with _OPENER.open(req, timeout=timeout or self.timeout) as resp:
                raw = resp.read()
        except urllib.error.HTTPError as e:
            raise BackendError(f"{method} {path}: HTTP {e.code} {e.reason}") from None
        except (urllib.error.URLError, OSError) as e:
            raise BackendError(f"{method} {path}: {type(e).__name__}: {e}") from None
        try:
            return json.loads(raw) if raw.strip() else {}
        except ValueError:
            raise BackendError(f"{method} {path}: reply is not JSON") from None

    def store(self, rec: Record) -> str:
        self._call("/ingest", {
            "namespace": rec.namespace, "source": rec.source_id, "text": rec.content,
            "metadata": {**rec.metadata, "kind": rec.kind, "created_at": rec.created_at},
            "replace": True,
        })
        return rec.source_id

    def recall(self, query: str, namespace: str, k: int) -> list[Evidence]:
        if k <= 0:
            return []
        data = self._call("/retrieve", {"namespace": namespace, "query": query, "top_k": k},
                          timeout=min(self.timeout, 5))
        hits = data.get("hits") if isinstance(data, dict) else None
        out = []
        for hit in (hits if isinstance(hits, list) else [])[:k]:
            if not isinstance(hit, dict):
                continue
            source, text = hit.get("source"), hit.get("text")
            if not isinstance(source, str) or not isinstance(text, str) or not text.strip():
                continue
            scores = {k2: v for k2, v in hit.items()
                      if k2.endswith("score") and isinstance(v, (int, float))}
            out.append(Evidence(source_id=source, content=text.strip(), scores=scores,
                                adapter=self.name))
        return out

    def delete(self, source_id: str, namespace: str) -> bool:
        self._call("/source", {"namespace": namespace, "source": source_id}, method="DELETE")
        return True

    def list(self, namespace: str) -> list[Record]:
        # memila has no listing endpoint. The canonical local copy that every
        # write keeps (writes.py) is what `memila-brain list` reads instead.
        raise BackendError("the memila backend cannot list records; the canonical local copy can")
