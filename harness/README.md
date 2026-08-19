# harness/

Design notes for `niceclaude`, written so a future session — on a different
machine, a different OS, or months later — can pick up the thread without
re-deriving anything or re-running experiments that already have answers.

Everything here is technical and public-safe.

## Read these in order

| File | What it holds |
|---|---|
| `design-decisions.md` | The control law, and every significant decision with its rationale and rejected alternatives |
| `platform-findings.md` | Empirical facts about Claude Code hooks and `/usage`, with the measurements behind them |
| `open-questions.md` | What is unverified, what is guessed, and what to do next |
| `test-matrix.md` | The behaviours that must hold, and how to re-check them |
| `windows-handoff.md` | **Self-contained brief for an agent on Windows** — the one platform still unverified |
| `windows-results.md` | Created by whoever runs the Windows checks |
| `freeze-validation.md` | Evidence for the 3-hour freeze |

## The one-paragraph summary

`niceclaude` paces Claude Code background work against its own usage windows so
unattended work yields budget to foreground work. Unix `nice` lowers a process's
scheduling priority so it yields CPU; this lowers a session's *budget* priority
so it yields tokens. A `PreToolUse` hook compares consumption against a
time-proportional "pace line" and, when the agent is running hot, **sleeps** —
freezing the agent in place until the line catches up.

## Shape of the system

```
niceclaude watch          tier 2, a daemon: polls `claude -p /usage` every 60s,
                          appends raw+parsed to usage.jsonl, publishes a raw
                          snapshot to state.json. Makes no decisions.

niceclaude-hook           tier 1, the hot path: runs on every PreToolUse and
                          SubagentStart, resolves this folder's policy, does
                          the pace-line arithmetic, sleeps if hot. ~20ms.

niceclaude on/off/status  policy CLI. Folder-scoped, re-read every tool call,
                          so changes reach a running agent.
```

The split exists because decisions are per-folder but usage is account-global:
one snapshot serves many folders under different policies, and the expensive
poll happens once a minute rather than once per tool call.

## State on disk

```
<data>/usage.jsonl     append-only history, raw stdout + parsed fields
<data>/state.json      latest snapshot (no decisions in it, by design)
<data>/policy.json     folder rules; re-read on every hook invocation
<data>/hook.log        brake/release events — the only record of overnight behaviour
<data>/daemon.pid      pidfile
<config>/settings.json optional --settings fragment; `install` registers the
                       hook in ~/.claude/settings.json (CLAUDE_CONFIG_DIR)
```

`<data>` is `~/.local/share/niceclaude` on POSIX, `%LOCALAPPDATA%\niceclaude` on
Windows. Override with `NICECLAUDE_DIR`.

## Machine state left behind by development

Two things were changed outside the repo and should be known about before
anything is trusted or torn down:

1. **The hook is installed globally** in `~/.claude/settings.json`. This started
   as an out-of-repo hand edit; `niceclaude install` now does it, so it is no
   longer machine state that differs from a clean setup — see *Why the hook is
   installed globally* in the top-level README for the reasoning, which is the
   one that was written here first: safe because the default policy paces
   nothing, so the hook fires everywhere and does nothing unless a folder is
   explicitly paced.

   The original file is still backed up at
   `~/.claude/settings.json.bak-preniceclaude`, from the hand edit. To undo, run
   `niceclaude uninstall` (which removes only our entries), or `niceclaude global
   off` to neutralize the hook without touching settings, or `NICECLAUDE_OFF=1`
   to exempt one session.
2. **`/config/workspace/niceclaude` is a paced folder** (`m0=5`, `m1=8`,
   `--model opus`). `niceclaude list` shows every rule; `niceclaude off <dir>`
   removes one.

## If you are resuming work here

1. Read `open-questions.md` first — it is the live edge of the work.
2. `niceclaude check` re-runs the current parser over the entire stored history.
   Run it before trusting anything; `/usage` output has already changed format
   once during development.
3. `test-matrix.md` can be re-run in a few minutes and costs no tokens.
