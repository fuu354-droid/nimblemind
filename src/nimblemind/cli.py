"""The `nimblemind` command line.

Hook commands (`hook session-start`, `hook session-end`) always exit 0 and
never print an error into the session: a failing memory tool must not turn
into a failing session. Every other command reports plainly and exits
non-zero on failure.

Commands that approve a write or widen what is approved (`pending approve`,
`store --approve`, `migrate`, `approval allow`, `trust`) require a person at
a terminal: they read a typed confirmation
from an interactive stdin and refuse without one. An agent driving a shell
has no terminal, so it can queue writes but cannot approve its own. The
scripted alternative is a standing grant from `approval allow`, which lives
in the data home (DESIGN.md section 7).
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from nimblemind import __version__, approval
from nimblemind import config as config_mod


class NotATerminal(SystemExit):
    pass


def confirm(question: str) -> str:
    """The approver's name, after a typed `yes` at a real terminal."""
    if not (sys.stdin.isatty() and sys.stdout.isatty()):
        raise NotATerminal(
            "this approves a write, which needs a person at a terminal; "
            "run it in a terminal, or grant the path with `nimblemind approval allow <path>`")
    answer = input(f"{question} Type yes to approve: ").strip().lower()
    if answer != "yes":
        raise SystemExit("not approved")
    return approval.who()


def _cfg(args) -> config_mod.Config:
    cfg = config_mod.load(getattr(args, "cwd", None) or Path.cwd())
    if getattr(args, "namespace", None):
        cfg.data["namespace"] = args.namespace
    for note in cfg.notes:
        print(f"note: {note}", file=sys.stderr)
    return cfg


def _out(args, obj, text: str) -> None:
    if getattr(args, "json", False):
        print(json.dumps(obj, ensure_ascii=False, indent=2))
    else:
        print(text)


# ---- hooks ---------------------------------------------------------------

def cmd_hook(args) -> int:
    from nimblemind import distill, harness, model, recall

    try:
        raw = "" if sys.stdin is None or sys.stdin.isatty() else sys.stdin.read()
    except Exception:  # closed, binary or broken stdin: no payload, still exit 0
        raw = ""
    try:
        if model.is_child():
            return 0
        probe = harness.load(args.harness or "claude-code").read_payload(raw)
        cfg = config_mod.load(probe.get("cwd") or None)
        h = harness.load(args.harness or cfg["harness"])
        payload = h.read_payload(raw)
        if args.event == "session-start":
            block = recall.assemble(cfg)
            if block:
                print(h.session_start_output(block))
        elif args.event == "session-end":
            distill.hook(cfg, payload)
    except Exception as e:  # never break the session; leave a trace instead
        try:
            from nimblemind.fsutil import append_line
            from nimblemind.paths import data_home
            from nimblemind.records import now_iso

            append_line(data_home() / "logs" / "hook.log",
                        f"{now_iso()} {args.event}: {type(e).__name__}: {e}")
        except Exception:
            pass
    return 0


def cmd_distill(args) -> int:
    from nimblemind import distill

    cfg = config_mod.load(args.cwd or None)
    try:
        distill.distill(cfg, args.transcript, args.session_id)
    except Exception as e:
        distill._log(cfg, f"error {args.session_id}: {type(e).__name__}: {e}")
        return 1
    return 0


# ---- records -------------------------------------------------------------

def cmd_store(args) -> int:
    from nimblemind import writes

    cfg = _cfg(args)
    content = args.text if args.text is not None else sys.stdin.read()
    if not content.strip():
        raise SystemExit("nothing to store")
    by = confirm(f"Store this {args.kind} in namespace {cfg.namespace!r}?") if args.approve else ""
    r = writes.write(cfg, "cli", args.kind, content, source_id=args.source_id or "",
                     metadata={}, approved_by=by)
    _out(args, r.__dict__, f"{r.state} {r.source_id}" + (f": {r.detail}" if r.detail else ""))
    return 0 if r.state in ("stored", "pending") else 1


def cmd_recall(args) -> int:
    from nimblemind import writes
    from nimblemind.backends import BackendError

    cfg = _cfg(args)
    try:
        hits = writes.recall_backend(cfg).recall(args.query, cfg.namespace, args.k)
    except BackendError as e:
        _out(args, {"state": f"unavailable({e})", "hits": []}, f"unavailable: {e}")
        return 1
    _out(args, {"state": "ok" if hits else "empty", "hits": [h.to_dict() for h in hits]},
         "\n---\n".join(f"[{h.source_id}] {h.content}" for h in hits) or "(no matches)")
    return 0


def cmd_list(args) -> int:
    from nimblemind import writes

    cfg = _cfg(args)
    recs = writes.canonical(cfg).list(cfg.namespace)
    _out(args, [r.to_dict() for r in recs],
         "\n".join(f"{r.created_at}  {r.kind:<8} {r.source_id}" for r in recs) or "(no records)")
    return 0


def cmd_delete(args) -> int:
    from nimblemind import writes

    from nimblemind.backends import BackendError

    cfg = _cfg(args)
    try:
        existed = writes.delete(cfg, args.source_id)
    except BackendError as e:
        print(f"deleted locally; the backend did not confirm: {e}")
        return 1
    print(("deleted " if existed else "no such record ") + args.source_id)
    return 0 if existed else 1


# ---- approval ------------------------------------------------------------

def cmd_pending(args) -> int:
    from nimblemind import writes

    cfg = _cfg(args)
    if args.action == "list":
        items = writes.pending(cfg)
        _out(args, items, "\n".join(
            f"{e['id']}  {e['path']:<8} {e['record']['kind']:<8} {e['record']['source_id']}"
            for e in items) or "(nothing pending)")
        return 0
    if not args.ids:
        raise SystemExit(f"pending {args.action} needs at least one id")
    if args.action == "show":
        for e in writes.pending(cfg):
            if e["id"] in args.ids:
                print(f"== {e['id']} ({e['path']}, {e['record']['source_id']})\n{e['record']['content']}")
        return 0
    if args.action == "reject":
        for i in args.ids:
            writes.reject(cfg, i)
            print(f"rejected {i}")
        return 0
    shown, failed = {}, 0
    for i in args.ids:
        try:
            e = writes.load_pending(cfg, i)
        except ValueError as err:
            failed += 1
            print(f"FAIL {i}: {err}")
            continue
        rec = e["_record"]
        shown[i] = rec.digest
        print(f"== {i}  path {e['path']}  kind {rec.kind}  {rec.source_id}  "
              f"sha256 {rec.digest[:12]}\n{rec.content}")
    if not shown:
        return 1
    by = confirm(f"Approve the {len(shown)} write(s) shown above into namespace {cfg.namespace!r}?")
    for i, digest in shown.items():
        try:
            r = writes.approve(cfg, i, by=by, expect_sha256=digest)
            print(f"stored {r.source_id}" + (f": {r.detail}" if r.detail else ""))
        except ValueError as e:
            failed += 1
            print(f"FAIL {i}: {e}")
    return 1 if failed else 0


def cmd_held(args) -> int:
    from nimblemind import writes

    cfg = _cfg(args)
    items = writes.held(cfg)
    if args.action == "list":
        _out(args, items, "\n".join(
            f"{e['id']}  {e['verdict']:<11} {e['record']['source_id']}: {e['report'][:80]}"
            for e in items) or "(nothing held)")
        return 0
    for e in items:
        if e["id"] in args.ids:
            if args.action == "show":
                print(f"== {e['id']} ({e['verdict']}: {e['report']})\n{e['record']['content']}")
            elif args.action == "discard":
                (cfg.layout().held / e["_file"]).unlink(missing_ok=True)
                print(f"discarded {e['id']}")
    return 0


def cmd_approval(args) -> int:
    from nimblemind.config import APPROVAL_PATHS

    cfg = _cfg(args)
    if args.action == "show":
        for p in APPROVAL_PATHS:
            print(f"{p:<9} {cfg['approval'][p]}")
        print(f"grants file: {config_mod.grants_file()}")
        return 0
    if args.path not in APPROVAL_PATHS:
        raise SystemExit(f"approval {args.action} needs a write path: {', '.join(APPROVAL_PATHS)}")
    if args.action == "allow":
        confirm(f"Store every `{args.path}` write without asking from now on?")
    config_mod.set_grant(args.path, args.action == "allow")
    print(f"{args.path}: {'allow' if args.action == 'allow' else 'ask'} "
          f"({config_mod.grants_file()})")
    return 0


def cmd_audit(args) -> int:
    from nimblemind import writes

    cfg = _cfg(args)
    problems = writes.audit(cfg)
    print("\n".join(problems) if problems else "audit clean")
    return 1 if problems else 0


def cmd_flush(args) -> int:
    from nimblemind import writes

    cfg = _cfg(args)
    sent, left = writes.flush(cfg)
    print(f"sent {sent}, still unsynced {left}")
    return 1 if left else 0


# ---- config --------------------------------------------------------------

def cmd_trust(args) -> int:
    path = Path(args.path).resolve() if args.path else None
    if path and path.is_dir():
        path = path / ".nimblemind.toml"
    if path is None:
        from nimblemind.paths import find_project_config

        path = find_project_config(Path.cwd())
    if not path or not path.is_file():
        raise SystemExit("no project config found")
    if args.revoke:
        print(("untrusted " if config_mod.untrust(path) else "was not trusted: ") + str(path))
        return 0
    shown = path.read_bytes()
    print(shown.decode("utf-8", errors="replace"))
    confirm(f"Trust {path} to run the commands and choose the backend shown above?")
    try:
        digest = config_mod.trust(path, expect=shown)
    except ValueError as e:
        raise SystemExit(str(e)) from None
    print(f"trusted {path} (sha256 {digest[:12]})")
    return 0


def cmd_config(args) -> int:
    from nimblemind import paths

    cfg = _cfg(args)
    _out(args, {"data_home": str(paths.data_home()), "user_config": str(paths.user_config()),
                "project_config": str(cfg.project_file or ""), "project_trusted": cfg.project_trusted,
                "effective": cfg.data},
         json.dumps({"data_home": str(paths.data_home()), "user_config": str(paths.user_config()),
                     "project_config": str(cfg.project_file or ""),
                     "project_trusted": cfg.project_trusted, "effective": cfg.data},
                    ensure_ascii=False, indent=2))
    return 0


def cmd_migrate(args) -> int:
    from nimblemind import migrate

    cfg = _cfg(args)
    if not args.dry_run:
        confirm(f"Import every passing file in {args.source} into namespace {cfg.namespace!r}?")
    lines = migrate.run(cfg, Path(args.source).expanduser(), args.kind, dry_run=args.dry_run)
    print("\n".join(lines) or "(no files)")
    return 1 if any(ln.startswith("FAIL") for ln in lines) else 0


# ---- parser --------------------------------------------------------------

def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(prog="nimblemind", description=__doc__.split("\n\n")[0])
    ap.add_argument("--version", action="version", version=f"nimblemind {__version__}")
    sub = ap.add_subparsers(dest="cmd", required=True)

    def common(p, json_flag=True):
        p.add_argument("--namespace", help="override the configured namespace")
        p.add_argument("--cwd", help="resolve the project config from here (default: current directory)")
        if json_flag:
            p.add_argument("--json", action="store_true", help="machine-readable output")
        return p

    h = sub.add_parser("hook", help="harness hook entry points (always exit 0)")
    h.add_argument("event", choices=["session-start", "session-end"])
    h.add_argument("--harness", help="override the configured harness")
    h.set_defaults(fn=cmd_hook)

    d = sub.add_parser("distill", help="distill one transcript (what session-end runs detached)")
    d.add_argument("--transcript", required=True)
    d.add_argument("--session-id", default="unknown")
    d.add_argument("--cwd", default="")
    d.set_defaults(fn=cmd_distill)

    s = common(sub.add_parser("store", help="store a note through isolation and approval"))
    s.add_argument("--text", help="content (default: read stdin)")
    s.add_argument("--kind", default="note", choices=["note", "journal", "answer"])
    s.add_argument("--source-id")
    s.add_argument("--approve", action="store_true", help="approve now (needs a terminal)")
    s.set_defaults(fn=cmd_store)

    r = common(sub.add_parser("recall", help="recall by query"))
    r.add_argument("query", nargs="?", default="")
    r.add_argument("-k", type=int, default=5)
    r.set_defaults(fn=cmd_recall)

    common(sub.add_parser("list", help="list stored records")).set_defaults(fn=cmd_list)

    dl = common(sub.add_parser("delete", help="delete one record"), json_flag=False)
    dl.add_argument("source_id")
    dl.set_defaults(fn=cmd_delete)

    p = common(sub.add_parser("pending", help="writes awaiting approval"))
    p.add_argument("action", choices=["list", "show", "approve", "reject"])
    p.add_argument("ids", nargs="*")
    p.set_defaults(fn=cmd_pending)

    hd = common(sub.add_parser("held", help="writes the isolation check held"))
    hd.add_argument("action", choices=["list", "show", "discard"])
    hd.add_argument("ids", nargs="*")
    hd.set_defaults(fn=cmd_held)

    ap_ = sub.add_parser("approval", help="show or change which write paths store without asking")
    ap_.add_argument("action", choices=["show", "allow", "revoke"])
    ap_.add_argument("path", nargs="?")
    ap_.add_argument("--cwd")
    ap_.add_argument("--namespace")
    ap_.set_defaults(fn=cmd_approval)

    common(sub.add_parser("audit", help="check stored records against the approval log"),
           json_flag=False).set_defaults(fn=cmd_audit)
    common(sub.add_parser("flush", help="resend records the backend missed"),
           json_flag=False).set_defaults(fn=cmd_flush)

    t = sub.add_parser("trust", help="trust a project config to run commands")
    t.add_argument("path", nargs="?")
    t.add_argument("--revoke", action="store_true")
    t.set_defaults(fn=cmd_trust)

    common(sub.add_parser("config", help="show the effective config and locations")).set_defaults(fn=cmd_config)

    m = common(sub.add_parser("migrate", help="import from an older on-disk layout"), json_flag=False)
    m.add_argument("source")
    m.add_argument("--kind", default="journal", choices=["journal", "answer"])
    m.add_argument("--dry-run", action="store_true")
    m.set_defaults(fn=cmd_migrate)
    return ap


def _utf8_io() -> None:
    """UTF-8 on stdin and stdout whatever the locale: hook payloads and
    injected context are UTF-8 on every platform, and Windows pipes default
    to a legacy code page that cannot even encode most of it."""
    for stream in (sys.stdin, sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError, OSError):
            pass


def main(argv: list[str] | None = None) -> int:
    _utf8_io()
    args = build_parser().parse_args(argv)
    try:
        return args.fn(args)
    except config_mod.ConfigError as e:
        if args.cmd == "hook":
            return 0
        print(f"config error: {e}", file=sys.stderr)
        return 2
