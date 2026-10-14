# nimblemind: design

Session memory and a curiosity queue for AI coding agents, as a standalone
package. Extracted from a private workspace's `brain` and `curiosity` scripts
(the origin task is rag-08 there). This file is the contract the first release
is built and reviewed against.

## 1. Goals and non-goals

Goals, each one an acceptance criterion in section 12:

1. Installs and runs standalone on Linux, macOS and Windows, for any user, in
   any install location, with no dependency on the origin repository and no
   file under `~/.claude/`.
2. Works with zero infrastructure: the default backend is files on disk, and
   store, recall-by-query and delete complete with no memila, Qdrant or
   network present.
3. Every persistence path goes through one choke point that runs one
   isolation check and one write approval. A write that fails either is held
   or rejected, never stored and flagged.
4. Every injected block has a cap, all blocks share one total budget, and the
   source label counts against it.
5. Pluggable both ways: other memory backends plug in under one interface, and
   other tools reach this one through a small API and a CLI with JSON output.

Non-goals, ruled in the origin task:

- No retrieval, chunking, embedding, reranking or evaluation of its own. The
  local backend matches and orders; ranking quality is the backend's concern.
- memila is one optional backend. Nothing imports it, requires it or defaults
  to it.
- "What to ask next" and "when to stop" in curiosity stay rule based in this
  package, never delegated to a retrieval framework.
- No tenant identity field anywhere in the data shapes. Isolation is by
  namespace, and a namespace is an opaque string.

## 2. Runtime and packaging

- Python 3.12 or newer, standard library only. Installed with
  `uv tool install nimblemind` (or `pipx install`), which provisions 3.12
  into an isolated environment on any platform, so the system python's
  version never matters: the console script's own launcher points at that
  environment, and the hooks call nothing else.
- One package, `nimblemind`, one console script, `nimblemind`, and
  `python -P -m nimblemind` as the equivalent for hosts where the scripts
  directory is not on `PATH` (`-P` keeps the session's directory, which is
  the project, off `sys.path`).
- Child processes (the distill child, model commands, isolation commands)
  run in a private directory under the data home (`<home>/run/`, mode
  0700): never in the session's project, whose files a relative lookup
  would find, and never in the shared system temp directory, where another
  local user could plant files. Executables must resolve to an absolute
  path, looked up without the current directory. Prompt text never goes on
  a command line (on Windows an npm-installed `claude` is a `.cmd` shim run
  through `cmd.exe`); it goes on stdin, and the model name must be a plain
  token (letters, digits, `.`, `_`, `:`, `-`, not starting with `-`),
  whether it goes to `claude` or into an `argv` command's `{model}`. "The current directory" is the logical one (`$PWD` when it names
  the same directory), so a symlinked directory stays unresolved.
- No shell wrapper. Every hook command is the console script itself, so the
  same command line works under bash, zsh, PowerShell and cmd.
- File locking uses `fcntl` on POSIX and `msvcrt` on Windows behind one helper.
  File writes are atomic (write to a sibling temp file, then `os.replace`).
- All text files are UTF-8, written with `newline="\n"`.
- CI runs the full suite on ubuntu, macos and windows, on Python 3.12, 3.13
  and 3.14 (the newest stable release).

## 3. Locations

Nothing is hardcoded to a user, a machine or a repository. Each location
resolves in one order: environment variable, then config file, then platform
default.

| what | env var | platform default |
|---|---|---|
| data home | `NIMBLEMIND_HOME` | Linux: `$XDG_DATA_HOME/nimblemind` (`~/.local/share/nimblemind`); macOS: `~/Library/Application Support/nimblemind`; Windows: `%LOCALAPPDATA%\nimblemind` |
| user config | `NIMBLEMIND_CONFIG` | Linux: `$XDG_CONFIG_HOME/nimblemind/config.toml` (`~/.config/...`); macOS: same directory as data home; Windows: `%APPDATA%\nimblemind\config.toml` |
| project config | none | `.nimblemind.toml`, found by walking up from the session's working directory, stopping at the filesystem root |

The data home also reads `home = "<path>"` from the user config, between
the environment variable and the platform default.

Every location is an absolute path. A relative value would resolve against
whatever directory the process runs in, which for a hook is the session's
project: a relative platform base (`XDG_DATA_HOME`, `XDG_CONFIG_HOME`,
`APPDATA`, `LOCALAPPDATA`) is ignored, as the XDG Base Directory spec says,
and a relative `NIMBLEMIND_HOME`, `NIMBLEMIND_CONFIG` or `home` is a config
error, as is a relative home directory (`HOME`, `USERPROFILE`) whenever a
default location is derived from it (hooks stay silent). A relative path
given on the command line (`migrate`, `trust`, `distill --transcript`) joins
the logical current directory, like `--cwd`.

Data home layout:

```
<home>/
  namespaces/<ns>/
    records/            canonical copy of every stored record, plus index.json
    pending/            writes awaiting approval; never read by recall
    held/               writes the isolation check refused or could not judge
    unsynced.json       records a non-local backend has not received yet
  curiosity/<ns>/
    gaps.jsonl          the gap queue, unless a project config points elsewhere
    answers/
  approvals.jsonl       append-only approval log (section 7)
  approval.toml         standing `allow` grants (section 7)
  pending.jsonl         anchors for queued writes (section 7)
  trusted-projects.json project configs a person trusted, by content hash
  logs/                 write.log, distill.log, hook.log
  locks/
```

`<ns>` is the namespace escaped to one portable path component: only
lowercase ASCII letters, digits, `-` and `_` pass through, everything else is
a fixed-width `%` escape (uppercase too, since Windows and macOS compare names
case-insensitively), and an `ns-` prefix keeps names like `con` off the
names Windows reserves.

## 4. Configuration

TOML, read with the standard library's `tomllib`, so a person can comment a
hand-written config and no third-party parser is needed.
Precedence, lowest first: built-in defaults, user config, project config,
environment variables (`NIMBLEMIND_<SECTION>_<KEY>`, for scalar keys only).
Environment values for the narrowing sections apply at the user's level, so
a project still narrows them, and `approval` is never read from the
environment.
A project config arrives with whatever repository a person cloned, so it is
not trusted the way the user's own file is:

- In the narrowing sections (`approval`, `budget`, and the pattern parts of
  `isolation`) it can only narrow what the user config allows, never widen
  it, and a value there of the wrong type or an unknown key is a config
  error rather than silently ignored (an `allow` is ignored and named on
  stderr, as in the user config): the stricter value wins, so a repository cannot grant itself write
  access or a bigger injection. This holds even for a trusted project.
- Every key that runs a command, loads code, chooses where data is sent or
  which memory pool a session reads (`namespace`, `harness`, `backend`, the
  distiller and researcher, `isolation.command`, and the whole of `learn`
  and `curiosity`, whose settings decide when model calls run and what is
  proposed as a rule) applies only once a person has trusted that exact
  file with `nimblemind trust`, at a terminal, which records the hash of
  the bytes it showed, keyed by the file's absolute path as found (not
  resolved). Every entry point (CLI `--cwd`, hook payload, distill child)
  normalizes its directory the same way and never resolves symlinks, so a
  symlinked directory is not taken for the project it points to. A `.nimblemind.toml` that is a symlink is never trusted, so one
  repository cannot borrow another's trust or namespace. Loading reads the file once and checks that same
  read's hash, so an edit, a pulled change or a swap drops trust until it is
  given again. An untrusted file's other keys still apply, and the dropped ones are
  named on stderr.

```toml
namespace = "default"
harness = "claude-code"

[backend]
name = "local"

[distiller]
command = "claude-cli"
model = "haiku"

[researcher]
command = "claude-cli"
model = "sonnet"

[journal]
sections = ["Decisions", "Rulings", "Pitfalls", "Surprises", "Changes",
            "Unfinished", "Lessons learned", "Improvements found"]
recall_sections = ["Unfinished", "Pitfalls"]
core_sections = ["Unfinished"]   # [] injects the whole newest journal
max_words = 500
# language = "en"      unset: no language rule
banned_chars = []

[budget]
total_chars = 6000
beliefs_chars = 1200
core_chars = 2500
recall_chars = 2000
prompt_chars = 3000

[approval]             # "ask" or "deny" only; see section 7 for "allow"
# distill = "ask"      every path defaults to "ask"; naming a path here
# research = "ask"     explicitly is a tightening that a standing grant
# gap = "ask"          does not override, so leave a path out to let
# provider = "ask"     `nimblemind approval allow` apply to it
# cli = "ask"
# learn = "ask"        beliefs update automatically unless this says ask

[isolation]
checks = ["builtin-secrets"]

[learn]                # section 15
auto = true
similarity = 0.35
recur_min = 2
max_new = 25

[curiosity]
confidence_floor = 70
min_priority = 1.0
max_attempts = 2
verify_window = 5
```

`namespace` names the memory pool a session reads and writes. A project config
sets it per repository, so every worktree of one project shares one pool, and
two projects on one machine never read each other's memory.

## 5. Storage interface

```python
class Backend(Protocol):
    name: str
    def store(self, rec: Record) -> str: ...            # returns source_id
    def recall(self, query: str, namespace: str, k: int) -> list[Evidence]: ...
    def delete(self, source_id: str, namespace: str) -> bool: ...
    def list(self, namespace: str) -> list[Record]: ...
```

`Record` is `{source_id, namespace, kind, content, metadata, created_at}`;
`kind` is `journal`, `answer` or `note`. `Evidence` is the shared evidence shape
from the origin workspace: `{source_id, content, scores, adapter,
collected_at, state}`, with `state` one of `ok`, `empty`,
`unavailable(reason)`. Neither shape carries a tenant field.

Every backend call takes the namespace explicitly. A backend that cannot
enforce namespaces itself does not qualify.

Backends:

- `local` (default): records as files under `namespaces/<ns>/records/`, an
  `index.json` with id, kind, created time and a lowercase token set per
  record. `recall` returns records sharing at least one query token, ordered
  by token overlap count and then recency; an empty query returns the newest
  records. That is matching and ordering, not a retrieval model, and it is the
  floor the non-goal allows.
- `memila`: HTTP to a memila instance (`POST /ingest`, `POST /retrieve`,
  `DELETE /source`),
  configured by `MEMILA_URL` and `MEMILA_API_KEY`, then `~/.config/memila.env`.
  `forbidden_endpoints` in its config refuses named hosts and ports before a
  socket opens (the origin workspace uses it to keep personal memory off a
  company service). Only `http` and `https` URLs are accepted or opened.
  An entry is `host:port`, `:port` (any host), a bare `host` (every port),
  `[v6]` or `[v6]:port` (IPv6 always bracketed), where a host is an IPv4
  address or a hostname of letters, digits, `-` and `.`, and a port is
  1-65535;
  anything else (not a list of strings, a URL, spaces, a wildcard, a
  non-numeric port) is a config error; a URL whose host part carries any percent-encoding
  is refused, since urllib decodes it before connecting. Addresses compare
  in one canonical spelling: an
  IPv4-mapped address is its IPv4 address, and an unspecified address
  (`0.0.0.0`, `::`) counts as this machine's loopback.
- Third party: `name = "plugin"` with `target = "package.module:Class"` loads any
  class implementing the protocol. This is the inward provider direction.

Every stored record keeps a canonical copy in the local record store,
whatever backend is configured, so listing, auditing and rebuilding never
depend on a server. A non-local backend receives the record as well; a
failed send is listed in `unsynced.json` and `nimblemind flush` retries it.
Records are ordered by a microsecond creation stamp, so "the newest journal"
is well defined even for two writes inside one second.

## 6. The write path

Every persistence goes through `write(path, namespace, kind, content,
metadata)`. `path` is one of `distill`, `research`, `gap`, `provider`, `cli`.
Nothing else in the package calls `Backend.store`.

1. Isolation check (section 8). Refused: the content goes to `held/` with the
   check's report, logged `quarantine`, never stored. Check could not run:
   `held/` as unscanned, logged `gate unavailable`. Fail-open applies to the
   session (a hook never blocks start or end), never to the write.
2. Approval (section 7). `deny`: rejected and logged. `ask`: the content goes
   to `pending/`, its hash anchored in `pending.jsonl`, logged `pending`.
   `allow`: step 3, then an approval entry naming the grant.
3. Storage: the canonical local copy, then the configured backend when it
   is not `local`. A failure of that second send leaves the record stored
   locally and listed for `nimblemind flush`.

`pending/` and `held/` are never read by recall, on any backend.

## 7. Write approval

- Each write path has a level: `ask`, `allow` or `deny`. The default for
  every path is `ask`, except `learn`, whose default is automatic (section
  15.1). A background run is never treated as authorized by
  being in the background; only a person's command or a standing grant for
  that exact path stores anything.
- `allow` is a standing grant, and it lives only in `<home>/approval.toml`,
  written by `nimblemind approval allow <path>` at a terminal
  (`approval revoke` removes it, `approval show` lists the levels). It is not
  read from the user config or the environment: their locations are chosen
  by variables (`NIMBLEMIND_CONFIG`, `XDG_CONFIG_HOME`, `HOME`) an agent can
  set on its own command line, so an `allow` read from either is one the
  agent can grant itself. The grants file sits in the data home, so with the
  `local` backend moving it moves the store with it, and an agent that
  points the data home elsewhere only approves writes into a store no real
  session reads (a non-local backend: see the limits below). A grant is
  also bound to the user config in effect when it was given (its path is
  recorded beside it): the tightenings a grant defers to live in that
  file, so when a different user config is read (another
  `NIMBLEMIND_CONFIG` or `XDG_CONFIG_HOME`), no grant applies and writes
  wait for approval. Moving the user config means granting again. The user
  config and a project config can set `ask` or `deny` (tightening); an
  `allow` there is ignored and named on stderr.
- `nimblemind pending list|show <id>|approve <id>...|reject <id>...`.
- Every command that approves or widens approval (`pending approve`,
  `store --approve`, `migrate`, `approval allow`, `trust`) reads a typed
  `yes` from an interactive terminal and refuses without one. An agent
  driving a shell has no terminal, so it can queue writes but cannot approve
  its own.
- `pending approve` shows each entry (path, kind, source id, hash, content)
  before asking, and stores exactly what was shown; `store --approve`
  shows the record the same way before it asks. Every text the CLI prints
  shows each non-printable or default-ignorable character (controls,
  format characters such as zero-width spaces and Unicode tag characters,
  bidirectional overrides, line separators, variation selectors, Hangul
  fillers; newline and tab excepted) as a visible `\uXXXX` escape,
  so content cannot hide part of itself; `--json` output carries the exact
  text. It refuses an entry that
  does not match its anchor in `pending.jsonl` (namespace and hash, written
  when it was queued, outside the entry file), one whose hash differs from
  the one shown, one whose path is now `deny`, and one that no longer passes
  isolation. An approved or rejected entry's anchor is marked consumed, so
  the entry cannot be replayed.
- The hash an approval binds covers the whole record (source id, namespace,
  kind, content, metadata, creation time), not the content alone: a note
  approved as a note cannot be stored as a journal.
- `approvals.jsonl` gets one line per stored record: `{source_id, namespace,
  path, sha256, approved_by, approved_at, rule}`. `approved_by` is the OS user
  for a command, or `grant:<home>/approval.toml#<path>` for a standing grant.
  The line is written after the record is stored, so a crash between the two
  leaves a record `audit` reports, never an approval for nothing.
- `nimblemind audit` verifies every record file on disk (not the index)
  against the log: a record with no entry, whose hash differs from its entry,
  or a file that is not a readable record of its namespace, is reported and
  exits non-zero. That is what makes approval verifiable rather than asserted.
- The approval sits after isolation on purpose: a person is never asked to
  approve something the gate would refuse anyway.
- Limits, stated rather than implied: everything above constrains what this
  CLI does, and it fully protects a store only when the store lives in the
  data home (the `local` backend). It does not defend against a process
  running as the same user that edits the data home's files directly
  (including through symlinks), or that fabricates a terminal (a pty
  wrapper).
- With a non-local backend the store lives outside the data home, so a
  same-user agent can reach it without this CLI at all: it can read the
  backend's URL and key from the user's own files and send a write directly,
  or point this CLI at a data home it controls. Approval here then
  constrains only this CLI's own writes. Protecting a shared remote store
  takes enforcement in the service on its write endpoint (`nimblemind
  serve`, section 10, or the backend itself), which is the same server-side
  rule section 8 states for isolation.

## 8. Isolation check

`isolation.checks` is a list; a write passes only if every check passes.

- `builtin-secrets`: common credential shapes (private key blocks, provider
  key prefixes, bearer tokens). On by default.
- `builtin-patterns`: `deny = ["regex", ...]` per namespace, plus
  cross-namespace protection: each namespace may declare `identity` strings,
  and content written to namespace A that contains namespace B's identity
  strings is refused.
- `command`: `argv = ["...", "{file}"]`. The content is written to a temp
  file, the argv runs with `{file}` substituted, exit 0 passes, exit 1 refuses
  with stdout as the report, anything else means the check could not run. This
  is how the origin workspace keeps its own `check_output.py` without the
  package knowing its layout. `{project}` expands to the directory of the
  project config that applies, and only when that config is trusted: it
  names the repository's own files, which never run without trust. A check
  naming it with no trusted project config is `unavailable`, so the write
  is held. A project-relative checker is written
  `["python", "{project}/scripts/check.py", "{file}"]`. The executable must
  be an absolute path or found on `PATH`; anything else is `unavailable`. A
  trusted project's `command` runs in addition to the user's, never
  instead of it.

The same checks run on every path. A multi-client deployment runs them in
the service on its write endpoint (`nimblemind serve`, section 10), not in
each client.

## 9. Injection budget

At session start the package injects one block, assembled in this order and
counted together against `budget.total_chars`:

1. The source label line, `[nimblemind <namespace>]`, which counts.
2. Beliefs: those that apply (section 15.8), whole items only, capped at
   `beliefs_chars`: challenged and revised ones first, then the person's,
   then by confidence.
3. Core memory: the newest approved journal's `journal.core_sections`
   (default `["Unfinished"]`), capped at `core_chars`. An empty
   `core_sections` list injects the whole journal, as v0.1.0 did.
4. Recall: up to `k` older records' `recall_sections`, capped at
   `recall_chars`.
5. A truncation note when anything was cut, which counts too.

Each cap is a hard cut with a note, never a silent trim. Over the total,
recall is dropped first, core is cut next, beliefs are cut last (whole
items, from the end). The per-prompt hook (section 10) has its own
`prompt_chars` cap. No cap a person sets may be above `total_chars`, and
config validation refuses that; a cap left at its default follows a lowered
total down instead.

Why core is no longer the whole newest journal: recency is not relevance.
The newest journal's decisions and pitfalls are about whatever that session
did, which is often not this one; its `Unfinished` section is what a resumed
task needs. Everything else in it stays reachable through recall and the
prompt hook, which query by what the session is actually doing.

## 10. Doors

Harness hooks, chosen by `harness`:

- `nimblemind hook session-start`: the block of section 9, in the harness's
  injection format.
- `nimblemind hook session-end`: detaches a distill process and returns. The
  same child runs `learn` (section 15.5) after the distill when
  `learn.auto` is true.
- `nimblemind hook prompt`: per-prompt recall, the prompt as the query, on
  every backend including `local`:
  1. A prompt with fewer than `budget.prompt_min_tokens` (default 3)
     distinct tokens injects nothing.
  2. Beliefs whose statement and `Applies when` share at least
     `learn.match_min` (default 2) distinct content tokens (section 15.3)
     with the prompt come first, then up to `budget.prompt_k` recalled
     records that are neither beliefs nor gaps, all under
     `[nimblemind <namespace> prompt]` and capped at `budget.prompt_chars`.
     The `local` backend is queried with the prompt's content tokens and a
     record must share `match_min` of them; any other backend is given the
     prompt text (its first 2000 characters) and ranks it itself. Over the
     cap, related records are dropped whole from the end, then beliefs;
     when not even one record fits whole, the best match is cut to the room
     left. Every cut is named in a note.
  3. Nothing already injected into the same session (by session start or an
     earlier prompt) is injected again. The session's injected ids live in
     `<home>/run/sessions/<sha256 of the session id>.json`, ids only, and
     files older than seven days are removed on the next hook run.
  4. Silent on any failure, as every hook is.

Harness adapters translate payloads and transcripts: `claude-code` (hook JSON,
JSONL transcript) and `generic` (plain text on stdin, text out). An adapter is
a small class; adding Codex or Hermes is adding one.

Model calls go through `distiller.command` and `researcher.command`:
`claude-cli` (the `claude -p` CLI with a model alias, never a dated build) or
an `argv` list for any command that reads a prompt on stdin and prints
text. Set to `none`, distill is skipped and journals come only from
`nimblemind store`; `learn` then drafts no revision (section 15.5) and
`research` refuses to run. `researcher.tools` (default
`["WebSearch", "WebFetch"]`) names the tools a `claude-cli` researcher may
use; an `argv` researcher ignores it.

Outward API (the provider direction): `nimblemind.api` exposes `store`,
`recall`, `delete`, `gaps_add`, `gaps_list` and `beliefs_list`, every
write routed through section 6 on path `provider`. A provider stores notes
only: journals and answers carry metadata the learning loop reads as
evidence, which another tool must not be able to mint. The CLI mirrors it with
`--json`. A Hermes-style memory provider built on this API is the first
external integration test, under `integrations/hermes/`; it is a test, not a
dependency.

## 11. Curiosity

Curiosity is one half of the learning loop and is specified there
(section 15): where questions come from, how they are ranked, when research
stops and how an answer is verified. The earlier plan (a queue researched
lowest confidence first) is replaced, not shipped. Two points from it stand:

- Commit, push and notification steps are not in the package. A
  `curiosity.after_run` argv hook lets a project run its own after
  `nimblemind research` (trusted projects only, section 4). It runs with no
  shell, its executable resolved like a model command's, in the trusted
  project's directory, under the researcher's timeout.
- `curiosity.gaps_file` is withdrawn: gap questions are records and go
  through the write path like every other record, and their lifecycle lives
  in the data home (section 15.2). A project that wants the queue in its
  tree reads it with `nimblemind gap list --json`.

## 12. Acceptance

1. Clean environment test, run in CI on all three operating systems: fresh
   temp home, no network, no memila. Store, recall by query, delete, each
   asserting the result.
2. A distill or research write with default config lands in `pending/`, is
   invisible to recall, and becomes visible only after `pending approve`;
   `audit` passes after it and fails after the stored file is edited.
3. A write refused by an isolation check, and one whose check cannot run,
   both end in `held/` and never reach the backend; a write to namespace A
   containing namespace B's identity string is refused.
4. Session-start injection never exceeds `total_chars`, including label and
   truncation note, across a generated set of oversized journals and
   beliefs.
5. No path under `~/.claude/` is written by any test (asserted by running the
   suite with `HOME` pointed at a temp directory and checking it afterwards).
6. The origin workspace's hooks run on the package with its project config
   and pass its own suite.
7. A Hermes-style provider stub stores and recalls through `nimblemind.api`.
8. Evolution, end to end: a lesson seen once is a `hypothesis`; the same
   lesson from independent sessions makes it `supported` and then
   `established`; a session that was shown the belief and only repeats it
   adds nothing. Sessions where it applied and the problem it addresses
   happened anyway make it `challenged`; the drafted revision supersedes it
   with narrower conditions, the old belief is `retired` with its history
   kept, and the very next session-start block carries the revision, not
   the old belief.
9. A pinned belief (a person's) facing counter-evidence stays at least
   `supported`, is never revised or retired by the loop, and raises a
   `contradiction` gap instead.
10. A belief marked stale by a `Changes` item is not trusted at session
    start until new `for` evidence arrives.
11. Signals and curiosity: a pitfall recurring in two journals makes one
    gap and a third merges into it; a `contradiction` gap outranks a
    low-confidence `manual` gap of default impact; a gap is parked after
    `max_attempts` weak answers and reopens on new evidence; an answered
    gap is `verified` after `verify_window` clean journals and `recurred`
    when its signal returns; pending and held journals produce nothing.
12. Prompt hook on the `local` backend injects matching beliefs and records
    within `prompt_chars`, never repeats an id already injected in the same
    session, and prints nothing when it fails.
13. Session start injects applying beliefs and the newest journal's
    `core_sections`, not the whole journal, within `total_chars`; no belief
    write bypasses isolation, and `audit` covers every belief record.

## 13. Migration from the origin workspace

`nimblemind migrate --from <dir>` (or `migrate <dir>`) imports journals and
answers from an older layout (the origin's `~/.claude/brain/journals` and
`~/.claude/curiosity`).
Imported files already passed that workspace's gate, so each gets an approval
entry with `rule: "migrated"` and its hash, and the source is left in place
for the person to delete. Quarantined and unscanned files import into `held/`.
Imported journals carry no `Surprises`, `Changes` or `Belief checks` section; they
still feed the recurring and lesson signals, which read only the sections
present.

## 14. Release order

1. Core: locations, config, backend interface with `local` and `memila`,
   write path, approval, isolation, budget, `claude-code` and `generic`
   harness adapters, session hooks, CLI, migrate. Tag `v0.1.0`.
2. The learning loop (section 15), the prompt hook, the outward API and the
   Hermes test. Tag `v0.2.0`.
3. The origin workspace switches to the pinned tag: its hooks call the
   console script, its project config carries what used to be constants, and
   its own `brain_*` and `curiosity_*` scripts are removed.

## 15. The learning loop

Storing and finding is not learning, and neither is piling up rules. What
this section specifies is knowledge that evolves: every session is evidence
for or against what is already believed, a belief's confidence moves with
that evidence, a counter-example narrows or replaces it, and the next
session works from the corrected version. Curiosity is the part that turns
uncertainty and contradiction into questions worth checking.

Every decision here is a rule this package runs (what counts as evidence,
what is independent, how confidence moves, when a belief is challenged or
retired, what to ask next and when to stop), never a model's judgement or a
retrieval framework's (section 1). A model only writes text: a journal, a
belief's wording, a research answer.

Three limits hold everywhere in this section:

- Repetition is not support. The same claim copied into many journals, a
  session restating a belief it was shown, and the model's own conclusions
  do not prove a belief. Only independent sessions, real outcomes and
  surviving counter-examples move confidence (section 15.3).
- A person's word is not overruled by experience. A belief the person
  stated (section 15.1, `pinned`) is never weakened, revised or retired by
  the loop; evidence against it becomes a question for the person.
- Confidence is not authority. A belief is context injected into a session.
  Nothing reads a belief to decide approval, isolation, trust, budgets or
  any other permission, so a better-supported belief never grants an agent
  more than it had.

### 15.1 Beliefs

A belief is a record of kind `belief`, source id `belief:B-<8 hex>`:

```
# Belief B-<8 hex>
Statement: <one sentence: what holds, or what to do>
Applies when: <the situation>
Not when: <the exceptions, or "none">
```

Its metadata: `origin` (`person`, `ruling`, `lessons`, `research` or
`revision`), `supersedes` (the belief it revises, or empty) and `evidence`
(the source ids it was first drawn from). A revision is a new record that
supersedes the old one; the old one stays stored, retired, so the history of
what was believed and why it changed is kept.

`pinned` beliefs are the person's own: `origin` `person` (`nimblemind
belief add`, path `cli`) or `ruling` (an item of a journal's
`learn.ruling_sections`, in a journal a person stored: its approval entry
names a person, not a standing grant or an automatic path; a ruling in a
journal stored under a grant is an ordinary lesson). The loop adds evidence
to a pinned belief and reports it, but never lowers it below `supported`,
marks it stale, revises or retires it. Similarity cannot see negation
("always X" and "never X" share their tokens), so a person's ruling is only
ever matched to a belief that is already the person's: it never adopts a
learned belief, and becomes a belief of its own otherwise.

Beliefs are written on path `learn`, whose default level is automatic: a
belief write passes the isolation check like every write (section 6), is
logged in `approvals.jsonl` as `approved_by: "auto:learn"`, `rule: "auto"`,
and so `audit` covers it. This is the one path whose default is not `ask`
(section 7), because revising knowledge as evidence arrives is the point of
the loop. A person who wants to review every belief sets `approval.learn =
"ask"` (a tightening, so a project may set it too); `nimblemind approval
revoke learn` is not needed and does nothing.

### 15.2 Learning state

What is believed is derived, never accumulated. Every belief's evidence,
confidence, status and history, and every gap's lifecycle, are a function
of the stored records (journals, beliefs, gaps, answers) and the person's
event log, recomputed by every `learn` run. So for a given set of records,
the order in which they became visible (a journal approved a week after it
was queued, a run cut short by `max_new`, a research answer approved later)
cannot change what is derived. What the loop writes in response (a revision,
a question) does depend on what it had seen when it wrote it, as any
decision depends on when it was made; those records then become part of
what is derived from. The derived state is cached in
`<home>/learn/<ns>/state.json`; a cache that fails to load is reported by
the commands that read it (hooks stay silent) and rebuilt, losing nothing,
by the next run. `nimblemind learn --reset-state` is the derivation alone:
it writes no record.

What a run does (write a new belief, a gap, a revision) is a side effect,
recorded once in `<home>/learn/<ns>/done.jsonl`, so it happens once however
often the history is derived: a rejected gap is not asked again, a
journal's lessons are proposed once, and each signal is marked when its gap
is written or merged (one past a run's cap waits for the next run). A person's own actions on a belief
(`belief retire`, `belief stale`) are appended to
`<home>/learn/<ns>/events.jsonl` and are part of what is derived from.

All three files hold ids, enumerated words, numbers and timestamps only;
loading refuses any other value. Free text is a record, and a record goes
through section 6.

### 15.3 Evidence

`learn` reads stored journals only, never `pending/` or `held/`, oldest
first, each once. An item is one `- ` line of a section (not `- none`). Two
texts are similar when their content tokens (the local backend's tokens,
minus a fixed list of English stop words, minus non-CJK tokens under three
characters) have a Jaccard index of at least `learn.similarity` (default
0.35). A belief's signature is its statement and `Applies when`.

Every belief has a birth time: the session it came from for a lesson or
ruling, otherwise the moment it was written; and an end: its successor's
birth, or a person retiring it. No journal after the end is evidence about
it (its successor's, if it applies). An independent restatement counts
whenever it happened, before the birth included, except from what a belief
was drawn from, as it stands at derivation time: a research belief is never
supported by any session that raised its question (every occurrence of the
gap, including journals approved later) or by a session shown the answer it
came from, and a revision never by a session that was a counter-example to
the belief it revises. A revision
takes over at its own birth (section 15.5).

The unit of independence is the session (the journal's `session_id`): one
session, one vote, however many journals it was distilled into. A session
was shown a belief when the distiller listed it (`beliefs_listed`) or a hook
injected it, or a record it was drawn from, into that session
(`beliefs_shown`, `records_shown`, from the per-session record of section
10). A journal is evidence about a belief in these ways, at most
one `for` and one `against` per session and belief:

| kind | direction | when |
|---|---|---|
| `restated` | for | an item of `learn.lesson_sections` is similar to the belief, and the belief was not listed to that session's distiller (a session that was shown the belief and repeats it is not independent) |
| `outcome` | for | the session was shown the belief, its `Belief checks` say `followed`, and no pitfall or surprise of that session is similar to the belief |
| `counter` | against | the belief was listed to the session (it applied) and a pitfall or surprise of that session is similar to it: it was in force and the problem it addresses happened anyway |
| `research` | for or against | a stored research answer (one a person approved or granted) at or above `confidence_floor` for a `contradiction` gap about the belief: against when the answer revises it, for otherwise; one per gap. An answer to a `not-holding` gap is about why the belief was skipped, not whether it is right, and is no evidence |

`violated` in `Belief checks` is not evidence about whether the belief is
right. It means the belief was not applied, which raises the `not-holding`
signal (section 15.6) instead.

Confidence, with `s` the `for` count and `c` the `against` count:

```
confidence = (s + 1) / (s + 2c + 2)
```

A counter-example weighs twice a support: a belief has to survive its
exceptions, not outnumber them.

### 15.4 Status

Recomputed after every piece of evidence, first match wins:

| status | when | injected |
|---|---|---|
| `retired` | superseded, retired by a person, or (not pinned) `c >= 2` and confidence under 0.25 | never |
| `stale` | marked for revalidation (section 15.5) and no `for` evidence since | as stale, at the prompt hook only |
| `challenged` | (not pinned) the newest evidence is `against` and confidence is under 0.6 | yes, marked challenged, until revised |
| `established` | `s >= 4` and confidence at least 0.8 | yes |
| `supported` | `s >= 2` and confidence at least 0.6 | yes |
| `hypothesis` | otherwise | revisions only; others at the prompt hook only |

A pinned belief is at least `supported` whatever its evidence.

Each status change is appended to the belief's history in the state with
its time and the evidence that caused it; `nimblemind belief history <id>`
prints the chain of versions and their transitions. A person's own actions
on a belief (`belief retire`, `belief stale`) are also appended to
`<home>/learn/<ns>/events.jsonl` (ids and times only), so a rebuilt state
keeps them.

A first observation becomes a hypothesis at once: an item of
`learn.lesson_sections` that no stored or pending belief covers becomes a
new belief (`origin: lessons`, or `ruling` from a ruling section of a
person's journal), and that journal is its first `restated` evidence. A
retired belief still covers: restating a refuted belief is not a first
observation and never brings it back as a new one. New
journals are handled oldest first, and a run stops once it has created
`learn.max_new` (default 25) beliefs; the rest wait for the next run, which
only spreads the writes: the derivation is the same either way.

### 15.5 Revision and revalidation

When a belief becomes `challenged`, the distiller drafts a revision from
the belief and the items of its `against` evidence:

```
Statement: ...
Applies when: <narrowed to where it held>
Not when: <the situations of the counter-examples>
```

The revision is written on path `learn` as a new belief (`origin:
revision`, `supersedes` the challenged one); the challenged belief is
retired by it. A belief is drafted at most one revision while one is stored
or waiting for approval (`approval.learn = "ask"`). A revision carries its predecessor's `for` evidence (what
held still held, now under narrower conditions) and none of its `against`,
and until it has evidence of its own it is injected at session start marked
`revised: verifying`, so the next session works from the corrected version
and its own outcome is the revision's first test. With `distiller.command = "none"` no revision is drafted: the
belief stays `challenged` and a `contradiction` gap asks why.

A journal item in `learn.change_sections` (default `Changes`: what changed
in the environment, a version, a dependency or a requirement) similar to a
belief that is not pinned marks it `stale`. So does `nimblemind belief stale
<id>`. A stale belief needs one new `for` before it is trusted again; until
then only the prompt hook shows it, marked stale.

### 15.6 Gaps: what to ask next and when to stop

A gap is a record of kind `gap` on path `gap` (default `ask`: the person
sees what the loop wants to spend research on), source id `gap:G-<8 hex>`:

```
# Gap G-<8 hex>
Question: <the question>
Signal: recurring | surprise | not-holding | contradiction | manual
```

Signals, each built from a fixed template, never by a model:

| signal | fires when | weight |
|---|---|---|
| `contradiction` | a belief becomes `challenged`, or evidence goes against a pinned belief | 5 |
| `not-holding` | a session reports a belief `violated` | 4 |
| `surprise` | an item in `learn.surprise_sections` (default `Surprises`) | 3 |
| `recurring` | a pitfall similar to pitfalls in at least `learn.recur_min - 1` other journals (default `recur_min` 2) | 2 |
| `manual` | `nimblemind gap add` | the given impact, default 2 |

A signal similar to an open gap (or, for the belief signals, about the same
belief) and of the same kind merges into it instead of adding one: every
matching journal is an occurrence, whenever it is from. A verified gap
absorbs a journal older than its answer (an old occurrence approved late)
and never reopens for it; a newer one is a new question. At most five new gaps per run, highest
weight first; the rest are written by later runs.

`research` ranks `open` and `recurred` gaps:

```
impact      = weight * (1 + log2(occurrences))
uncertainty = (100 - confidence) / 100     confidence: manual value, else 50,
                                           else the last answer's
priority    = impact * (0.5 + uncertainty) / cost
```

`cost` is the manual value, else `curiosity.cost.<signal>` (defaults:
contradiction 1, not-holding 1, recurring 1, surprise 2, manual 2). Impact
outweighs uncertainty: a very uncertain question nobody is blocked on ranks
under a moderately certain one that keeps costing sessions. Research stops
on a stored answer at or above `confidence_floor` (`answered`); under it
the attempt counts, and at `max_attempts` the gap is `parked` until its
signal recurs. An answer waiting for the person (pending or held) neither
answers the gap nor counts as an attempt, and the gap is not researched
again until the person decides on it. An answered gap is `verified` once `verify_window` later journals
pass without its signal, and `recurred` (open again) if the signal returns:
verified means the problem stopped, not that an answer exists.

A research answer is a record of kind `answer` on path `research` (default
`ask`), tagged as untrusted evidence, carrying its confidence and its
proposed `Belief` part, so its effect is the same whenever a person
approves it. A model that fails to answer leaves no record and no attempt.
Its `Belief` part, when present,
becomes a belief (`origin: research`, a hypothesis with no evidence of its
own: a model's conclusion does not support itself) or, naming a belief it
revises, a revision; a pinned belief is never revised this way. Only a
stored answer does any of this, and it also counts as `research` evidence
(section 15.3).

`curiosity.after_run` lets a trusted project run its own notification after
`research`.

### 15.7 Belief checks

The distiller's instructions list the beliefs that apply (`supported`,
`established`, `challenged`, revised, and pinned) and any other belief a
hook showed the session, in session start's order, capped at
`learn.check_chars` (default 1500), and the journal gains:

```
## Belief checks
- B-1a2b3c4d: followed | violated | n/a: <evidence from the session>
```

The section is required only when beliefs were listed. Lines naming an id
that was not listed, or another verdict, are dropped. The parsed result
(`belief_checks`, `{id: verdict}`), the ids listed (`beliefs_listed`) and
the ids the hooks injected (`beliefs_shown`) go into the journal's metadata,
so the approval that stores the journal covers them, and `learn` counts a
check only for a belief the session was shown.

### 15.8 Applying

Session start (section 9) injects applying beliefs, `challenged` and
revised ones marked, then the newest journal's core sections; stale beliefs
and hypotheses reach a session only through the prompt hook. The prompt hook
(section 10) adds beliefs whose signature shares `learn.match_min` content
tokens with the prompt, hypotheses included and marked as such.
`nimblemind belief report` lists every belief with status, confidence, `for`
and `against` counts and its last transition.

### 15.9 What is left out, and why

- `serve` (local HTTP over the API) leaves this release. No second client
  exists to share a store with, section 7 already states that a shared
  remote store needs enforcement in the service, and a network write
  endpoint is a larger attack surface than the CLI whose review took the
  whole of v0.1.0. It ships when a second client does.
- Similarity is token overlap, not meaning. A contradiction worded with no
  shared terms is not found; the cost is a missed signal, never a wrong
  write, and a person can always add evidence by hand (`gap add`, `belief
  add`, `belief stale`).
- Nothing is deleted. Retired beliefs and parked gaps stay stored and
  reported; deleting stays a person's command.
