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

from nimblemind.backends import BackendError
from nimblemind.records import Evidence, Record

DEFAULT_URL = "http://localhost:6452"
ENV_FILE = Path.home() / ".config" / "memila.env"


def _env_file() -> dict[str, str]:
    try:
        lines = ENV_FILE.read_text(encoding="utf-8").splitlines()
    except OSError:
        return {}
    pairs = (ln.split("=", 1) for ln in lines if "=" in ln and not ln.lstrip().startswith("#"))
    return {k.strip(): v.strip() for k, v in pairs}


_LOCAL = {"127.0.0.1", "::1"}


def _addresses(host: str) -> set[str]:
    """Every address `host` reaches, in one canonical spelling: an
    IPv4-mapped IPv6 address becomes its IPv4 address, and an unspecified
    address (0.0.0.0, ::), which connects to this machine, stands for the
    loopback addresses."""
    out: set[str] = set()
    try:
        infos = socket.getaddrinfo(host, None)
    except OSError:
        return out
    for info in infos:
        try:
            ip = ipaddress.ip_address(info[4][0].split("%", 1)[0])
        except ValueError:
            continue
        if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped:
            ip = ip.ipv4_mapped
        if ip.is_unspecified:
            out |= _LOCAL
        out.add(ip.compressed)
        if ip.is_loopback:
            out |= _LOCAL if ip.version == 4 or ip.compressed == "::1" else set()
    return out


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


def _entry(entry: str) -> tuple[str, int | None]:
    """A forbidden_endpoints entry as (host, port); port None means every
    port. Forms: "host:port", ":port", "[v6]:port", "host", "[v6]"."""
    entry = entry.strip()
    if entry.startswith("[") and entry.endswith("]"):
        return entry[1:-1], None
    head, sep, tail = entry.rpartition(":")
    if sep and tail.isdigit() and (head.startswith("[") or ":" not in head):
        return head.strip("[]"), int(tail)
    return entry.strip("[]"), None


def is_forbidden(url: str, forbidden: list[str]) -> bool:
    """Whether `url` would reach a forbidden endpoint. Anything that cannot
    be shown to be allowed counts as forbidden: a port that does not parse,
    and any percent-encoding in the host part, which urllib decodes before
    connecting while the comparison here would not."""
    try:
        parts = urllib.parse.urlsplit(url)
        if "%" in parts.netloc.rpartition("@")[2]:
            return True
        port = parts.port or (443 if parts.scheme == "https" else 80)
    except ValueError:
        return True
    host = parts.hostname or ""
    for entry in forbidden:
        f_host, f_port = _entry(entry)
        if f_port is not None and f_port != port:
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
        forbidden = spec.get("forbidden_endpoints") or []
        if not isinstance(forbidden, list) or not all(isinstance(e, str) for e in forbidden):
            raise BackendError("backend.forbidden_endpoints must be a list of strings")
        for e in forbidden:
            host, _port = _entry(e)
            if not e.strip() or "/" in e or e.strip().endswith(":") or (not host and _port is None):
                raise BackendError(f"backend.forbidden_endpoints entry {e!r} is not host, "
                                   "host:port or :port")
        return cls(url, key, float(spec.get("timeout", 30)), forbidden)

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
        # write keeps (writes.py) is what `nimblemind list` reads instead.
        raise BackendError("the memila backend cannot list records; the canonical local copy can")
