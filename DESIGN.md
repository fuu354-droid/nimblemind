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
- CI runs the full suite on ubuntu, macos and windows, on Python 3.12 and the
  newest stable release.

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
  distiller and researcher, `isolation.command`, `curiosity.gaps_file` and
  `curiosity.after_run`) applies only once a person has trusted that exact
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
sections = ["Decisions", "Rulings", "Pitfalls", "Unfinished",
            "Lessons learned", "Improvements found"]
recall_sections = ["Unfinished", "Pitfalls"]
max_words = 500
# language = "en"      unset: no language rule
banned_chars = []

[budget]
total_chars = 6000
core_chars = 3500
recall_chars = 2000

[approval]             # "ask" or "deny" only; see section 7 for "allow"
# distill = "ask"      every path defaults to "ask"; naming a path here
# research = "ask"     explicitly is a tightening that a standing grant
# gap = "ask"          does not override, so leave a path out to let
# provider = "ask"     `nimblemind approval allow` apply to it
# cli = "ask"

[isolation]
checks = ["builtin-secrets"]

[curiosity]
confidence_floor = 70
# gaps_file = "path/in/project"   unset: kept in the data home
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
  company service).
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
  every path is `ask`. A background run is never treated as authorized by
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
  session reads (a non-local backend: see the limits below). The user config
  and a project config can set `ask` or `deny` (tightening); an `allow`
  there is ignored and named on stderr.
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
2. Core memory: the newest approved journal, capped at `core_chars`.
3. Recall: up to `k` older records' `recall_sections`, capped at
   `recall_chars`.
4. A truncation note when anything was cut, which counts too.

Each cap is a hard cut with a note, never a silent trim. If the label plus
core exceeds the total, recall is dropped first and core is cut next. The
per-prompt recall hook (section 10) has its own `prompt_chars` cap, which is
also counted against `total_chars` per prompt. The always-injected core is
the one thing a person relies on staying small, so `core_chars` cannot be set
above `total_chars`, and config validation refuses that.

## 10. Doors

Harness hooks, chosen by `harness`:

- `nimblemind hook session-start`: recall, prints the harness's injection
  format.
- `nimblemind hook session-end`: detaches a distill process and returns.
- `nimblemind hook prompt` (v0.2.0): per-prompt recall with the prompt as
  the query, capped at `budget.prompt_chars` (the origin workspace's
  `/augment` hook moves here, as a backend call).

Harness adapters translate payloads and transcripts: `claude-code` (hook JSON,
JSONL transcript) and `generic` (plain text on stdin, text out). An adapter is
a small class; adding Codex or Hermes is adding one.

Model calls go through `distiller.command` and `researcher.command`:
`claude-cli` (the `claude -p` CLI with a model alias, never a dated build) or
an `argv` list for any command that reads a prompt on stdin and prints
text. Set to `none`, distill is skipped and journals come only from
`nimblemind store`.

Outward API (the provider direction): `nimblemind.api` exposes `store`,
`recall`, `delete`, `gaps.add`, `gaps.list`, all routing writes through
section 6. The CLI mirrors it with `--json`. `nimblemind serve` runs the same
API over local HTTP for a multi-client setup, with isolation and approval
enforced in the server. A Hermes memory provider built on this API is the
first external integration test, under `integrations/hermes/`; it is a test,
not a dependency.

## 11. Curiosity

- `nimblemind gap add "<q>" --confidence N [--context ...]`, `gap list`,
  `gap resolve`. Gaps live in `curiosity/<ns>/gaps.jsonl` unless
  `curiosity.gaps_file` names a project file. A gap add goes through the write
  path as path `gap`.
- `nimblemind research [--max N]`: researches the lowest confidence open
  gaps through `researcher.command`, under a cross-platform lock. Each answer
  is a write on path `research`. The answer is tagged as untrusted evidence in
  its stored text.
- Commit, push and notification steps are not in the package. A
  `curiosity.after_run` argv hook lets a project run its own (the origin
  workspace's git commit and chat labeling stay there).

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
   truncation note, across a generated set of oversized journals.
5. No path under `~/.claude/` is written by any test (asserted by running the
   suite with `HOME` pointed at a temp directory and checking it afterwards).
6. The origin workspace's hooks run on the package with its project config
   and pass its own suite.
7. A Hermes-style provider stub stores and recalls through `nimblemind.api`.

## 13. Migration from the origin workspace

`nimblemind migrate --from <dir>` (or `migrate <dir>`) imports journals and
answers from an older layout (the origin's `~/.claude/brain/journals` and
`~/.claude/curiosity`).
Imported files already passed that workspace's gate, so each gets an approval
entry with `rule: "migrated"` and its hash, and the source is left in place
for the person to delete. Quarantined and unscanned files import into `held/`.

## 14. Release order

1. Core: locations, config, backend interface with `local` and `memila`,
   write path, approval, isolation, budget, `claude-code` and `generic`
   harness adapters, session hooks, CLI, migrate. Tag `v0.1.0`.
2. Curiosity, the prompt hook, `serve`, the outward API and the Hermes test.
   Tag `v0.2.0`.
3. The origin workspace switches to the pinned tag: its hooks call the
   console script, its project config carries what used to be constants, and
   its own `brain_*` and `curiosity_*` scripts are removed.
