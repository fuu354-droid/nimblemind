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
import math
import json
import os
import re
import socket
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

from nimblemind.backends import BackendError
from nimblemind.records import Evidence, Record

DEFAULT_URL = "http://localhost:6452"
MAX_TIMEOUT = 86400  # the same cap config.MAX_TIMEOUT puts on every other timeout
ENV_FILE = Path.home() / ".config" / "memila.env"


def _env_file() -> dict[str, str]:
    # Fixed at import from the home directory; a relative one would read a
    # file from whatever directory the process runs in.
    if not ENV_FILE.is_absolute():
        return {}
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


def _build_opener() -> urllib.request.OpenerDirector:
    """HTTP and HTTPS only. `build_opener` would add the FTP, file and data
    handlers too, so a non-HTTP URL would reach a port (or a file) the
    forbidden check never saw. No proxy from the environment either: a proxy
    variable would route the request, and the API key, through a host the
    check never saw."""
    opener = urllib.request.OpenerDirector()
    for handler in (urllib.request.ProxyHandler({}), urllib.request.UnknownHandler(),
                    urllib.request.HTTPHandler(), urllib.request.HTTPSHandler(),
                    urllib.request.HTTPDefaultErrorHandler(), _NoRedirect(),
                    urllib.request.HTTPErrorProcessor()):
        opener.add_handler(handler)
    return opener


_OPENER = _build_opener()


_HOSTNAME = re.compile(r"^(?=.{1,253}$)[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?"
                       r"(?:\.[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?)*\.?$")


def _host_ok(host: str) -> bool:
    try:
        ipaddress.ip_address(host)
        return True
    except ValueError:
        return bool(_HOSTNAME.match(host))


def _port_ok(port: str) -> bool:
    return port.isascii() and port.isdigit() and 1 <= int(port) <= 65535


def _entry(entry: str) -> tuple[str, int | None]:
    """A forbidden_endpoints entry as (host, port), parsed strictly; port None
    means every port. Accepted: `host`, `host:port`, `:port`, `[v6]`,
    `[v6]:port`, where host is an IP address or a hostname of letters,
    digits, `-` and `.`, and port is 1-65535. Anything else raises
    ValueError, so a mistyped entry is a config error, never an entry that
    silently matches nothing."""
    if not isinstance(entry, str) or not entry or entry != entry.strip():
        raise ValueError(f"forbidden endpoint {entry!r} is not host, host:port or :port")
    if entry.startswith("["):
        close = entry.find("]")
        if close < 0 or not _is_v6(entry[1:close]):
            raise ValueError(f"forbidden endpoint {entry!r}: bracketed host must be IPv6")
        host, rest = entry[1:close], entry[close + 1:]
        if rest == "":
            return host, None
        if rest.startswith(":") and _port_ok(rest[1:]):
            return host, int(rest[1:])
        raise ValueError(f"forbidden endpoint {entry!r}: bad port")
    if entry.count(":") > 1:
        # Bare IPv6 is ambiguous (`::1:6452` is an address, not ::1 port
        # 6452), so IPv6 is always bracketed.
        raise ValueError(f"forbidden endpoint {entry!r}: write IPv6 as [addr] or [addr]:port")
    host, sep, port = entry.partition(":")
    if sep:
        if not _port_ok(port):
            raise ValueError(f"forbidden endpoint {entry!r}: port must be 1-65535")
        if host and not _host_ok(host):
            raise ValueError(f"forbidden endpoint {entry!r}: bad host")
        return host, int(port)
    if not _host_ok(host):
        raise ValueError(f"forbidden endpoint {entry!r}: bad host")
    return host, None


def _is_v6(text: str) -> bool:
    try:
        return isinstance(ipaddress.ip_address(text), ipaddress.IPv6Address)
    except ValueError:
        return False


def is_forbidden(url: str, forbidden: list[str]) -> bool:
    """Whether `url` would reach a forbidden endpoint. Anything that cannot
    be shown to be allowed counts as forbidden: a port that does not parse,
    and any percent-encoding in the host part, which urllib decodes before
    connecting while the comparison here would not."""
    try:
        parts = urllib.parse.urlsplit(url)
        if parts.scheme.lower() not in ("http", "https"):
            return True  # only HTTP(S) is ever opened; anything else is refused
        if "%" in parts.netloc.rpartition("@")[2]:
            return True
        port = parts.port or (443 if parts.scheme == "https" else 80)
    except ValueError:
        return True
    host = parts.hostname or ""
    for entry in forbidden:
        try:
            f_host, f_port = _entry(entry)
        except ValueError:
            return True  # an entry that cannot be read cannot show the URL allowed
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
        if urllib.parse.urlsplit(url).scheme.lower() not in ("http", "https"):
            raise BackendError(f"backend url {url!r} must be http or https")
        timeout = spec.get("timeout", 30)
        if isinstance(timeout, bool) or not isinstance(timeout, (int, float)) \
                or (isinstance(timeout, float) and not math.isfinite(timeout)) \
                or not 0 < timeout <= MAX_TIMEOUT:
            raise BackendError(f"backend.timeout must be a positive number of seconds, "
                               f"at most {MAX_TIMEOUT}")
        forbidden = spec.get("forbidden_endpoints") or []
        if not isinstance(forbidden, list) or not all(isinstance(e, str) for e in forbidden):
            raise BackendError("backend.forbidden_endpoints must be a list of strings")
        for e in forbidden:
            try:
                _entry(e)
            except ValueError as err:
                raise BackendError(f"backend.forbidden_endpoints: {err}") from None
        return cls(url, key, float(timeout), forbidden)

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
        except (urllib.error.URLError, OSError, ValueError, OverflowError) as e:
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
                      if k2.endswith("score") and isinstance(v, (int, float))
                      and not isinstance(v, bool)
                      and not (isinstance(v, float) and not math.isfinite(v))}
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
