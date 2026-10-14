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
import math
import os
import sys
from pathlib import Path

from nimblemind import __version__, approval
from nimblemind.fsutil import logical_cwd
from nimblemind import config as config_mod


class NotATerminal(SystemExit):
    pass


def confirm(question: str) -> str:
    """The approver's name, after a typed `yes` at a real terminal."""
    if not (sys.stdin.isatty() and sys.stdout.isatty()):
        raise NotATerminal(
            "this approves a write, which needs a person at a terminal; "
            "run it in a terminal, or grant the path with `nimblemind approval allow <path>`")
    answer = input(visible(f"{question} Type yes to approve: ")).strip().lower()
    if answer != "yes":
        raise SystemExit("not approved")
    return approval.who()


def _cfg(args) -> config_mod.Config:
    # A relative --cwd means relative to where the command runs.
    cfg = config_mod.load(_abs(getattr(args, "cwd", None) or logical_cwd()))
    if getattr(args, "namespace", None):
        cfg.data["namespace"] = args.namespace
    for note in cfg.notes:
        say(f"note: {note}", file=sys.stderr)
    return cfg


# What a terminal would not show as itself: anything Python calls
# non-printable (C0 and C1 controls, DEL, format characters such as
# zero-width spaces, word joiners, byte order marks, soft hyphens, Unicode tag
# characters and bidirectional overrides, line and paragraph separators),
# except newline and tab. A person approving a write must see exactly what
# will be stored; `\r\x1b[2K` or a run of tag characters would otherwise
# hide part of it.
def visible(text: str) -> str:
    return "".join(ch if ch in "\n\t" or (ch.isprintable() and not _ignorable(ord(ch)))
                   else _escape(ord(ch)) for ch in str(text))


def _escape(cp: int) -> str:
    # Fixed width, so an escape never runs into the text after it.
    return f"\\u{cp:04x}" if cp <= 0xFFFF else f"\\U{cp:08x}"


# Unicode's Default_Ignorable_Code_Point: characters a terminal draws as
# nothing even where Python calls them printable (variation selectors,
# combining grapheme joiner, Hangul fillers, Mongolian free variation
# selectors), so a run of them could carry a hidden message.
_IGNORABLE = ((0x00AD, 0x00AD), (0x034F, 0x034F), (0x061C, 0x061C), (0x115F, 0x1160),
              (0x17B4, 0x17B5), (0x180B, 0x180F), (0x200B, 0x200F), (0x202A, 0x202E),
              (0x2060, 0x206F), (0x3164, 0x3164), (0xFE00, 0xFE0F), (0xFEFF, 0xFEFF),
              (0xFFA0, 0xFFA0), (0xFFF0, 0xFFF8), (0x1BCA0, 0x1BCA3), (0x1D173, 0x1D17A),
              (0xE0000, 0xE0FFF))


def _ignorable(cp: int) -> bool:
    return any(lo <= cp <= hi for lo, hi in _IGNORABLE)


def say(*parts, file=None) -> None:
    print(*(visible(p) for p in parts), file=file)


def _jsonable(obj):
    """`obj` with every value strict JSON can carry: non-finite floats (TOML
    allows inf and nan, a backend may send them) and dates or times become
    their text, so `--json` output parses under any JSON parser."""
    if isinstance(obj, dict):
        return {str(k): _jsonable(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [_jsonable(v) for v in obj]
    if isinstance(obj, float) and not math.isfinite(obj):
        return str(obj)
    if obj is None or isinstance(obj, (str, int, float, bool)):
        return obj
    return str(obj)


def _json(obj) -> str:
    return json.dumps(_jsonable(obj), ensure_ascii=False, indent=2, allow_nan=False)


def _out(args, obj, text: str) -> None:
    if getattr(args, "json", False):
        # default=str: TOML dates and times are valid config values JSON has
        # no type for; they print as their ISO text instead of crashing.
        print(_json(obj))
    elif text:
        say(text)


# ---- hooks ---------------------------------------------------------------

def _abs(p) -> str:
    """A directory as every entry point names it: absolute and normalized
    (`..` folded), symlinks left as they are, and a relative path joined to
    the logical cwd (`os.path.abspath` would join it to the physical one).
    Trust is keyed by the path as found, so resolving here would let a
    symlink borrow another project."""
    p = os.fspath(p)
    return os.path.normpath(p if os.path.isabs(p) else os.path.join(logical_cwd(), p))


def _hook_cwd(payload: dict) -> str:
    """The payload's cwd, which must be absolute: a relative one would be
    resolved against this process rather than the session, so it is a failed
    call (silent), never a reason to run without the project config."""
    cwd = payload.get("cwd") or ""
    if not Path(cwd).is_absolute():
        raise ValueError(f"hook payload cwd is not an absolute path: {cwd!r}")
    # Normalized but not resolved, exactly as every CLI path treats its
    # directory, so the hook and the distill child find the same project
    # config, and a symlinked directory is not another project's path.
    return _abs(cwd)


def cmd_hook(args) -> int:
    from nimblemind import distill, harness, model, recall

    try:
        raw = "" if sys.stdin is None or sys.stdin.isatty() else sys.stdin.read()
    except Exception:  # closed, binary or broken stdin: a failed call, so silent
        return 0
    try:
        if model.is_child():
            return 0
        # The harness decides how the payload is read, so it is chosen
        # before the payload: --harness, else the config for this process's
        # directory (a harness runs its hooks in the session's project, so a
        # trusted project's own harness is found). The payload's cwd then
        # decides the config; if that config names another harness, the
        # payload is read again with it.
        try:
            name = args.harness or config_mod.load(_abs(logical_cwd()))["harness"]
        except config_mod.ConfigError:
            # The process directory's config is only a first guess; a broken
            # one there must not silence a payload naming a valid project.
            name = config_mod.load(None)["harness"]
        h = harness.load(name)
        payload = h.read_payload(raw)
        cfg = config_mod.load(_hook_cwd(payload))
        if not args.harness and cfg["harness"] != h.name:
            h = harness.load(cfg["harness"])
            payload = h.read_payload(raw)
            cfg = config_mod.load(_hook_cwd(payload))
        cfg.data["harness"] = h.name
        if args.event in ("session-start", "prompt"):
            sid = payload.get("session_id", "")
            if args.event == "session-start":
                block, ids = recall.assemble_with_ids(cfg)
                render = h.session_start_output
            else:
                block, ids = recall.prompt_block(cfg, payload.get("prompt", ""), sid)
                render = h.prompt_output
            if block:
                recall.remember(sid, ids)
                try:
                    print(render(block))
                    sys.stdout.flush()  # here, not at exit: a full or closed stdout is caught
                except (OSError, ValueError):
                    # What is still buffered would fail again at interpreter
                    # exit and turn the exit status non-zero; drop it.
                    sys.stdout = open(os.devnull, "w")
                    raise
        elif args.event == "session-end":
            distill.hook(cfg, payload, harness_name=h.name)
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

    # The project config always applies: from --cwd, else this directory.
    cfg = config_mod.load(_abs(args.cwd or logical_cwd()))
    if args.harness:
        cfg.data["harness"] = args.harness
    try:
        distill.distill(cfg, _abs(args.transcript), args.session_id)
    except Exception as e:
        distill._log(cfg, f"error {args.session_id}: {type(e).__name__}: {e}")
        return 1
    if cfg["learn"]["auto"]:
        from nimblemind import learn

        try:
            learn.run(cfg)
        except Exception as e:  # the journal is already handled; learning retries next time
            distill._log(cfg, f"learn error: {type(e).__name__}: {e}")
            return 1
    return 0


# ---- records -------------------------------------------------------------

def cmd_store(args) -> int:
    from nimblemind import writes

    cfg = _cfg(args)
    content = args.text if args.text is not None else sys.stdin.read()
    if not content.strip():
        raise SystemExit("nothing to store")
    # With --approve the prompt runs inside the write path, after its one
    # isolation check passes: never asked about content the check refuses.
    def ask(rec):
        # Shown exactly as `pending approve` shows an entry: what is
        # approved is what was on screen.
        say(f"== path cli  kind {rec.kind}  {rec.source_id}  sha256 {rec.digest[:12]}\n{rec.content}")
        return confirm(f"Store this {rec.kind} in namespace {rec.namespace!r}?")

    ask = ask if args.approve else None
    try:
        r = writes.write(cfg, "cli", args.kind, content, source_id=args.source_id or "",
                         metadata={}, confirm=ask)
    except ValueError as e:
        raise SystemExit(f"store: {e}") from None
    _out(args, r.__dict__, f"{r.state} {r.source_id}" + (f": {r.detail}" if r.detail else ""))
    return 0 if r.state in ("stored", "pending") else 1


def cmd_recall(args) -> int:
    from nimblemind import writes

    cfg = _cfg(args)
    try:
        hits = writes.recall_backend(cfg).recall(args.query, cfg.namespace, args.k)
    except Exception as e:  # BackendError, or anything a plugin backend raises
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
        say(f"deleted locally; the backend did not confirm: {e}")
        return 1
    say(("deleted " if existed else "no such record ") + args.source_id)
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
        items = [e for e in writes.pending(cfg) if e["id"] in args.ids]
        for i in sorted(set(args.ids) - {e["id"] for e in items}):
            say(f"FAIL {i}: no pending entry", file=sys.stderr)
        _out(args, items, "\n".join(
            f"== {e['id']} ({e['path']}, {e['record']['source_id']})\n{e['record']['content']}"
            for e in items))
        return 0 if len(items) == len(set(args.ids)) else 1
    if args.action == "reject":
        failed = 0
        for i in args.ids:
            try:
                writes.reject(cfg, i)
                say(f"rejected {i}")
            except ValueError as e:
                failed += 1
                say(f"FAIL {i}: {e}")
        return 1 if failed else 0
    shown, failed = {}, 0
    for i in args.ids:
        try:
            e = writes.preflight(cfg, i)
        except ValueError as err:
            failed += 1
            say(f"FAIL {i}: {err}")
            continue
        rec = e["_record"]
        shown[i] = rec.digest
        say(f"== {i}  path {e['path']}  kind {rec.kind}  {rec.source_id}  "
              f"sha256 {rec.digest[:12]}\n{rec.content}")
    if not shown:
        return 1
    by = confirm(f"Approve the {len(shown)} write(s) shown above into namespace {cfg.namespace!r}?")
    # Re-read the config after the prompt, as approve's re-checks promise: a
    # path set to deny, or an isolation rule added, while the prompt was open
    # applies to these writes.
    cfg_now = config_mod.load(_abs(getattr(args, "cwd", None) or logical_cwd()))
    cfg_now.data["namespace"] = cfg.namespace
    for i, digest in shown.items():
        try:
            r = writes.approve(cfg_now, i, by=by, expect_sha256=digest)
            say(f"stored {r.source_id}" + (f": {r.detail}" if r.detail else ""))
        except ValueError as e:
            failed += 1
            say(f"FAIL {i}: {e}")
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

    if not args.ids:
        raise SystemExit(f"held {args.action} needs at least one id")
    unknown = [i for i in args.ids if i not in {e["id"] for e in items}]
    for i in unknown:
        say(f"FAIL {i}: no held entry", file=sys.stderr)
    if args.action == "show":
        shown = [e for e in items if e["id"] in args.ids]
        _out(args, shown, "\n".join(
            f"== {e['id']} ({e['verdict']}: {e['report']})\n{e['record']['content']}"
            for e in shown))
        return 1 if unknown else 0
    for e in items:
        if e["id"] in args.ids:
            if args.action == "discard":
                (cfg.layout().held / e["_file"]).unlink(missing_ok=True)
                say(f"discarded {e['id']}")
    return 1 if unknown else 0


def cmd_approval(args) -> int:
    from nimblemind.config import APPROVAL_PATHS

    cfg = _cfg(args)
    if args.action == "show":
        levels = {p: cfg["approval"][p] for p in APPROVAL_PATHS}
        _out(args, {"levels": levels, "grants_file": str(config_mod.grants_file())},
             "\n".join(f"{p:<9} {lvl}" for p, lvl in levels.items())
             + f"\ngrants file: {config_mod.grants_file()}")
        return 0
    if args.path not in APPROVAL_PATHS:
        raise SystemExit(f"approval {args.action} needs a write path: {', '.join(APPROVAL_PATHS)}")
    if args.action == "allow":
        confirm(f"Store every `{args.path}` write without asking from now on?")
    config_mod.set_grant(args.path, args.action == "allow")
    level = _cfg(args)["approval"][args.path]
    say(f"{args.path}: {level} (grants: {config_mod.grants_file()})")
    if args.action == "allow" and level != "allow":
        say(f"note: the grant is recorded, but approval.{args.path} = \"{level}\" is set "
              f"explicitly in {config_mod.paths.user_config()} or the project config, and an "
              "explicit setting is a tightening that wins; remove it for the grant to apply")
    return 0


def cmd_audit(args) -> int:
    from nimblemind import writes

    cfg = _cfg(args)
    problems = writes.audit(cfg, args.namespace or None)
    _out(args, {"clean": not problems, "problems": problems},
         "\n".join(problems) if problems else "audit clean")
    return 1 if problems else 0


def cmd_flush(args) -> int:
    from nimblemind import writes

    cfg = _cfg(args)
    try:
        sent, left = writes.flush(cfg)
    except ValueError as e:
        say(f"error: {e}", file=sys.stderr)
        return 1
    say(f"sent {sent}, still unsynced {left}")
    return 1 if left else 0


# ---- config --------------------------------------------------------------

def cmd_trust(args) -> int:
    # Locations first: a refused one is a config error (exit 2) before a
    # person is ever asked, as every other command reports it.
    config_mod.paths.user_config()
    config_mod.paths.data_home()
    # Normalized, never resolved: resolving would follow a symlink to another
    # project's file or directory and trust that instead (trust is keyed by
    # the path as found).
    path = None
    if args.path:
        given = Path(_abs(Path(args.path).expanduser()))   # the logical directory, as everywhere
        path = given / ".nimblemind.toml" if given.is_dir() else given
    if path is None:
        from nimblemind.paths import find_project_config

        path = find_project_config(logical_cwd())
    if not path or not path.is_file():
        raise SystemExit("no project config found")
    if args.revoke:
        say(("untrusted " if config_mod.untrust(path) else "was not trusted: ") + str(path))
        return 0
    if path.is_symlink():
        raise SystemExit(visible(f"{path} is a symlink; trust the file it points to in its own project"))
    shown = path.read_bytes()
    say(shown.decode("utf-8", errors="replace"))
    confirm(f"Trust {path} to run the commands and choose the backend shown above?")
    try:
        digest = config_mod.trust(path, expect=shown)
    except ValueError as e:
        raise SystemExit(visible(str(e))) from None
    say(f"trusted {path} (sha256 {digest[:12]})")
    return 0


# ---- learning ------------------------------------------------------------

def cmd_learn(args) -> int:
    from nimblemind import learn

    cfg = _cfg(args)
    try:
        summary = learn.run(cfg, reset_state=args.reset_state)
    except learn.StateError as e:
        raise SystemExit(f"learn: {e}") from None
    _out(args, summary, " ".join(f"{k}={v}" for k, v in summary.items()))
    return 0


def cmd_research(args) -> int:
    from nimblemind import learn, model, research

    cfg = _cfg(args)
    try:
        out = research.run(cfg, args.max)
    except (model.ModelError, learn.StateError) as e:
        raise SystemExit(f"research: {e}") from None
    lines = [f"{r['gap']}: {r.get('status', '?')} (confidence {r['confidence']}) {r['answer']}"
             + (f"; belief {r['belief']}" if r.get("belief") else "")
             for r in out["results"]]
    if out["state"] == "busy":
        lines = ["another research run holds the lock"]
    if out.get("after_run"):
        lines.append(out["after_run"])
    _out(args, out, "\n".join(lines) or "(nothing ready to research)")
    return 0 if out["state"] == "ok" else 1


def _approver(args, label: str):
    if not getattr(args, "approve", False):
        return None

    def ask(rec):
        say(f"== path cli  kind {rec.kind}  {rec.source_id}  sha256 {rec.digest[:12]}\n{rec.content}")
        return confirm(f"Store this {label} in namespace {rec.namespace!r}?")
    return ask


def cmd_gap(args) -> int:
    from nimblemind import learn, research, writes

    cfg = _cfg(args)
    if args.action == "list":
        try:
            items = research.ranked(cfg)
        except learn.StateError as e:
            raise SystemExit(f"gap list: {e}") from None
        if not args.all:
            items = [i for i in items if i["status"] != "verified"]
        waiting = [e for e in writes.pending(cfg) if (e.get("record") or {}).get("kind") == "gap"]
        rows = [{k: v for k, v in i.items() if k != "_rec"} for i in items]
        _out(args, {"gaps": rows, "pending": [e["record"]["source_id"] for e in waiting]},
             "\n".join(f"{i['priority']:>7.2f}  {i['status']:<9} {i['signal']:<11} "
                       f"{i['source_id']}  {i['question']}" for i in rows)
             + (f"\n{len(waiting)} gap(s) awaiting approval (nimblemind pending list)" if waiting else "")
             or "(no gaps)")
        return 0
    if not args.question or not args.question.strip():
        raise SystemExit("gap add needs a question")
    for name, lo, hi in (("impact", 1, 5), ("cost", 1, 5), ("confidence", 0, 100)):
        v = getattr(args, name)
        if v is not None and not lo <= v <= hi:
            raise SystemExit(f"--{name} must be from {lo} to {hi}")
    taken = {r.source_id.removeprefix("gap:") for r, _ in learn.known_gaps(cfg)}
    gid = learn.new_id("G", taken)
    meta = {"signal": "manual", "evidence": []}
    for name in ("impact", "cost", "confidence"):
        if getattr(args, name) is not None:
            meta[name] = getattr(args, name)
    try:
        r = writes.write(cfg, "cli", "gap", learn.gap_text(gid, args.question, "manual"),
                         source_id=f"gap:{gid}", metadata=meta, confirm=_approver(args, "gap"))
    except ValueError as e:
        raise SystemExit(f"gap add: {e}") from None
    _out(args, r.__dict__, f"{r.state} {r.source_id}" + (f": {r.detail}" if r.detail else ""))
    return 0 if r.state in ("stored", "pending") else 1


def cmd_belief(args) -> int:
    from nimblemind import learn, writes

    cfg = _cfg(args)
    sid = ""
    if args.id:
        sid = args.id if args.id.startswith("belief:") else f"belief:{args.id}"
    try:
        if args.action in ("list", "report"):
            rows = learn.report(cfg)
            if args.action == "list" and not args.all:
                rows = [r for r in rows if r["status"] != "retired"]
            _out(args, rows, "\n".join(
                f"{r['bid']}  {r['status']:<11} {r['confidence']:.2f}  for {r['for']}  "
                f"against {r['against']}{'  pinned' if r['pinned'] else ''}  {r['statement']}"
                + (f" (applies when: {r['applies_when']}; not when: {r['not_when']})"
                   if args.action == "report" else "")
                for r in rows) or "(no beliefs)")
            return 0
        if args.action == "history":
            if not sid:
                raise SystemExit("belief history needs an id")
            chain = learn.history(cfg, sid)
            if not chain:
                raise SystemExit(f"no belief {sid}")
            lines = []
            for v in chain:
                lines.append(f"{v['source_id']}  {v['status']}: {v['statement']} "
                             f"(applies when: {v['applies_when']}; not when: {v['not_when']})")
                lines += [f"    {h['at']}  -> {h['status']}  ({h['cause']})" for h in v["history"]]
            _out(args, chain, "\n".join(lines))
            return 0
    except learn.StateError as e:
        raise SystemExit(f"belief {args.action}: {e}") from None
    if args.action in ("retire", "stale"):
        if not sid:
            raise SystemExit(f"belief {args.action} needs an id")
        if args.action == "retire":
            confirm(f"Retire {sid}? It stops being injected and checked.")
            ok = learn.retire(cfg, sid)
        else:
            ok = learn.mark_stale(cfg, sid)
        if not ok:
            raise SystemExit(f"no belief {sid} that can be changed")
        say(f"{args.action}d {sid}")
        return 0
    if not args.statement or not args.statement.strip():
        raise SystemExit("belief add needs --statement")
    taken = {r.source_id.removeprefix("belief:") for r in learn.stored(cfg, "belief")}
    bid = learn.new_id("B", taken)
    content = learn.belief_text(bid, args.statement, args.applies_when or "", args.not_when or "")
    try:
        # A person's belief: pinned, never revised or retired by the loop.
        r = writes.write(cfg, "cli", "belief", content, source_id=f"belief:{bid}",
                         metadata={"origin": "person", "supersedes": "", "evidence": []},
                         confirm=_approver(args, "belief"))
    except ValueError as e:
        raise SystemExit(f"belief add: {e}") from None
    _out(args, r.__dict__, f"{r.state} {r.source_id}" + (f": {r.detail}" if r.detail else ""))
    return 0 if r.state in ("stored", "pending") else 1


def cmd_config(args) -> int:
    from nimblemind import paths

    cfg = _cfg(args)
    _out(args, {"data_home": str(paths.data_home()), "user_config": str(paths.user_config()),
                "project_config": str(cfg.project_file or ""), "project_trusted": cfg.project_trusted,
                "effective": cfg.data},
         _json({"data_home": str(paths.data_home()), "user_config": str(paths.user_config()),
                "project_config": str(cfg.project_file or ""),
                "project_trusted": cfg.project_trusted, "effective": cfg.data}))
    return 0


def cmd_migrate(args) -> int:
    from nimblemind import migrate

    cfg = _cfg(args)
    source = args.from_dir or args.source
    if not source:
        raise SystemExit("migrate needs a source directory (positional, or --from)")
    from nimblemind.fsutil import path_kind

    from nimblemind.fsutil import list_suffix

    # Everything that would make migrate.run fail is checked before a person
    # is asked: a missing source, a file, a directory that cannot be listed.
    # A relative source joins the logical cwd, like every other path argument.
    src = Path(_abs(Path(source).expanduser()))
    try:
        if path_kind(src) != "dir":
            raise SystemExit(visible(f"FAIL {source}: not a directory"))
        list_suffix(src, ".md")
    except OSError as e:
        raise SystemExit(visible(f"FAIL {source}: cannot be listed: {e.strerror or e}")) from None
    if not args.dry_run:
        confirm(f"Import every passing file in {src} into namespace {cfg.namespace!r}?")
    lines = migrate.run(cfg, src, args.kind, dry_run=args.dry_run)
    say("\n".join(lines) or "(no files)")
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
    h.add_argument("event", choices=["session-start", "session-end", "prompt"])
    h.add_argument("--harness", help="override the configured harness")
    h.set_defaults(fn=cmd_hook)

    d = sub.add_parser("distill", help="distill one transcript (what session-end runs detached)")
    d.add_argument("--transcript", required=True)
    d.add_argument("--session-id", default="unknown")
    d.add_argument("--cwd", default="")
    d.add_argument("--harness", default="", help="the harness the hook used (set by session-end)")
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
    ap_.add_argument("--json", action="store_true", help="machine-readable output")
    ap_.set_defaults(fn=cmd_approval)

    common(sub.add_parser("audit", help="check stored records against the approval log")
           ).set_defaults(fn=cmd_audit)
    common(sub.add_parser("flush", help="resend records the backend missed"),
           json_flag=False).set_defaults(fn=cmd_flush)

    t = sub.add_parser("trust", help="trust a project config to run commands")
    t.add_argument("path", nargs="?")
    t.add_argument("--revoke", action="store_true")
    t.set_defaults(fn=cmd_trust)

    common(sub.add_parser("config", help="show the effective config and locations")).set_defaults(fn=cmd_config)

    ln = common(sub.add_parser("learn", help="update beliefs and questions from stored journals"))
    ln.add_argument("--reset-state", action="store_true",
                    help="rebuild the learning state from the stored records")
    ln.set_defaults(fn=cmd_learn)

    rs = common(sub.add_parser("research", help="research the highest priority gaps"))
    rs.add_argument("--max", type=int, default=3)
    rs.set_defaults(fn=cmd_research)

    g = common(sub.add_parser("gap", help="questions the loop is asking"))
    g.add_argument("action", choices=["list", "add"])
    g.add_argument("question", nargs="?")
    g.add_argument("--impact", type=int)
    g.add_argument("--cost", type=int)
    g.add_argument("--confidence", type=int)
    g.add_argument("--all", action="store_true", help="include verified gaps")
    g.add_argument("--approve", action="store_true", help="approve now (needs a terminal)")
    g.set_defaults(fn=cmd_gap)

    bl = common(sub.add_parser("belief", help="what it believes, how sure, and why"))
    bl.add_argument("action", choices=["list", "report", "history", "add", "retire", "stale"])
    bl.add_argument("id", nargs="?")
    bl.add_argument("--statement")
    bl.add_argument("--applies-when")
    bl.add_argument("--not-when")
    bl.add_argument("--all", action="store_true", help="include retired beliefs")
    bl.add_argument("--approve", action="store_true", help="approve now (needs a terminal)")
    bl.set_defaults(fn=cmd_belief)

    m = common(sub.add_parser("migrate", help="import from an older on-disk layout"), json_flag=False)
    m.add_argument("source", nargs="?")
    m.add_argument("--from", dest="from_dir", help="the source directory (same as the positional)")
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
    except (config_mod.ConfigError, config_mod.paths.LocationError) as e:
        if args.cmd == "hook":
            return 0
        say(f"config error: {e}", file=sys.stderr)
        return 2
    except OSError as e:
        # A data home that cannot be read is reported plainly, not as a
        # traceback (hooks never get here: cmd_hook catches everything).
        say(f"error: {e}", file=sys.stderr)
        return 1
