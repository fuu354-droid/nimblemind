# memila-brain

Session memory for AI coding agents. At the end of a session it distills the
transcript into a short journal; at the start of the next one it injects
where you left off. It runs with no server and no database, on Linux, macOS
and Windows, and nothing it stores bypasses an isolation check and your
approval.

- **Zero infrastructure.** The default backend is plain files under your data
  directory. A [memila](https://github.com/fuu354-droid/memila) instance, or
  any class implementing the four-method backend interface, can be plugged in.
- **One write path.** Every write passes an isolation check (credential
  shapes, your own deny patterns, cross-namespace identity strings, or any
  command you name), then approval, then storage.
- **You approve what it remembers.** Background writes wait in a pending
  queue until you approve them at a terminal, or until you grant that write
  path standing approval with `memila-brain approval allow <path>`. Every
  stored record is logged with its hash, and `memila-brain audit` checks the
  store against that log.
- **One injection budget.** The label, the last journal, older recall and any
  truncation note all count against one cap, and every cut is named.

## Install

Python 3.12 or newer, provisioned for you by
[uv](https://docs.astral.sh/uv/):

```
uv tool install git+https://github.com/fuu354-droid/memila-brain@v0.1.0
memila-brain --version
```

`pipx install git+https://...` works the same way.

## Wire it into Claude Code

In `~/.claude/settings.json` (or a project's `.claude/settings.json`):

```json
{
  "hooks": {
    "SessionStart": [{"hooks": [{"type": "command", "command": "memila-brain hook session-start"}]}],
    "SessionEnd":   [{"hooks": [{"type": "command", "command": "memila-brain hook session-end"}]}]
  }
}
```

Both hooks always exit 0. A failure is written to `logs/hook.log` under the
data home and never reaches the session.

Distilling uses the `claude` CLI with the `haiku` model alias by default. Any
other model works through a command that reads a prompt on stdin and prints
the answer:

```toml
[distiller]
command = "argv"
argv = ["ollama", "run", "llama3.1"]
```

## Everyday commands

```
memila-brain pending list            # what is waiting for approval
memila-brain pending show <id>
memila-brain pending approve <id>    # at a terminal; asks you to type yes
memila-brain recall "qdrant healthcheck"
memila-brain list
memila-brain delete <source_id>
memila-brain held list               # what the isolation check stopped
memila-brain audit                   # stored records vs the approval log
memila-brain config                  # effective config and where files live
```

`--json` gives machine-readable output on the read commands.

## Configuration

`memila-brain config` prints where your user config lives (for example
`~/.config/memila-brain/config.toml` on Linux). Every key and its default is
in [DESIGN.md](DESIGN.md#4-configuration). A small example:

```toml
namespace = "work"

[journal]
language = "English"
banned_chars = ["\u2014"]   # em dash

[isolation]
checks = ["builtin-secrets", "builtin-patterns"]
deny = ["(?i)project-codename"]
```

To store distilled journals without asking each time (at a terminal):

```
memila-brain approval allow distill
```

That grant lives in the data home, not in the config file, so nothing that
can only set environment variables or write a config file can grant it.

A repository can carry a `.memila-brain.toml` at its root. It can only
tighten approval, budget and isolation. Keys that run commands, choose the
backend or choose the namespace apply only after you trust that exact file:

```
memila-brain trust          # at a terminal, from inside the repository
```

Editing the file afterwards drops the trust until you trust it again.

## Moving from an older layout

```
memila-brain migrate ~/.claude/brain/journals --dry-run
memila-brain migrate ~/.claude/brain/journals
```

Files that pass the isolation check are imported and logged as approved by
you; marked or refused files go to `held/`. The source directory is not
changed.

## License

MIT
