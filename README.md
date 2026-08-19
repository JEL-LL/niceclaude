# niceclaude

Pace Claude Code background work against its own usage windows, so unattended
work yields budget to whatever you do in the foreground.

Unix `nice` lowers a process's scheduling priority so it yields CPU to more
important work. `niceclaude` lowers a Claude Code session's *budget* priority so
it yields tokens to your foreground sessions. Same idea, different resource.

## The pace line

Every usage window has a start, an end, and a percentage consumed. If you are
`f_t` of the way through the window's *time*, you should be at most `f_t` of the
way through its *budget*:

```
allowed(f_t) = m0 + f_t * (100 - m0 - m1)
```

`m0` is a starting grubstake (the pure diagonal would permit 0% at 0% elapsed,
so nothing could ever begin). `m1` is an end-of-window reserve, so you come in
under the wire rather than exactly on it.

Above the line, the agent sleeps until the line rises to meet it. Usage never
falls, so the wake time is solvable in closed form — but it is recomputed on
every poll rather than committed to, because your foreground session and any
sibling agents draw on the same account-global budget and can push it later
while you wait.

This needs no day-of-week policy. Skip a day and headroom accumulates on its
own; hammer it in the foreground and background work goes quiet until the line
catches up.

## Install

Not on PyPI yet — the release pipeline is in place but no version has been cut —
so install from git:

```bash
uv tool install "niceclaude[plot] @ git+https://github.com/JEL-LL/niceclaude"
```

Drop `[plot]` if you would rather not pull in matplotlib; everything except
`niceclaude plot` works without it.

### Updating a git install

`uv tool upgrade niceclaude` does **not** work here. It compares against an
index, finds no newer *version*, and reports *"Nothing to upgrade"* — the version
string in `pyproject.toml` has not moved, so from uv's point of view nothing has.
It never re-pulls the ref. The upgrade appears to succeed and you keep running
the old commit, which is a worse outcome than an error.

Re-run the install instead, with `--force`:

```bash
uv tool install --force "niceclaude[plot] @ git+https://github.com/JEL-LL/niceclaude"
niceclaude stop && niceclaude watch &   # the daemon holds the old code in memory
niceclaude install                      # idempotent; only writes if something changed
```

All three matter:

- `--force` is what makes uv discard the cached checkout and re-clone the ref.
- The **daemon is a long-lived process**, so it keeps running the old module until
  restarted. The hook is fine without this — it is a fresh process per tool call,
  so it picks up new code immediately.
- `niceclaude install` re-registers in case the hook path moved (a rebuilt tool
  venv can land elsewhere). It updates in place rather than adding a second
  registration, and prints `(already registered)` when there was nothing to do.

Verifying you actually moved is the awkward part, because `uv tool list` prints
the version from `pyproject.toml` — which is exactly the string that does not
change between commits. The revision uv resolved is recorded here:

```bash
grep -o 'rev = "[^"]*"' "$(uv tool dir)/niceclaude/uv-receipt.toml"
```

Pin a ref by appending it — `...@v0.1.0` for a tag, `...@<sha>` for a commit.
A `--force` reinstall is then the only thing that moves you off it, which is the
point of pinning.

If your install came from a local clone rather than the URL (the receipt says
`directory = ...` instead of a git ref), the equivalent is
`uv tool install --force ".[plot]"` from inside that clone after a `git pull`.

To hack on it, install from a clone instead:

```bash
git clone https://github.com/JEL-LL/niceclaude && cd niceclaude
uv tool install --editable ".[plot]"   # source edits take effect with no reinstall
```

Then wire it up:

```bash
niceclaude install                     # registers the hook in ~/.claude/settings.json
niceclaude on ~/projects/nightly --model opus
niceclaude watch                       # the poller (run it under systemd, a
                                       # container entrypoint, or just &)
```

That is the whole setup. `claude` is then launched normally, from anywhere — the
hook is registered once and every session consults the policy, so turning pacing
on or off is only ever `niceclaude on` / `niceclaude off`. Folders with no
matching rule are untouched: the hook answers *"is this folder paced?"* from
`policy.json` alone, with no snapshot read, no subprocess and no network, so it
is a genuine no-op there (~20ms).

`install` **merges** into `~/.claude/settings.json` rather than writing it. Your
`model`, `permissions`, and your own hooks — including other hooks on the same
two events — all survive, and a file it cannot parse is refused rather than
overwritten. Running it twice updates in place instead of registering the hook
twice. `niceclaude uninstall` removes exactly what it added and leaves policy and
logs alone.

To exempt a single session, set `NICECLAUDE_OFF` to any non-empty value:

```bash
NICECLAUDE_OFF=1 claude          # this session is not paced, wherever it runs
```

That is the one exemption settings files cannot express, and the reason a global
install is safe — see below.

Pure Python. No `jq`, no shell dependency, so it runs the same on Linux, macOS
and Windows.

Dependency-free on Linux and macOS. On Windows it declares `tzdata`, because
Windows ships no timezone database and `/usage` prints IANA zone names — without
it the reset time has to be inferred as machine-local, and when that inference
is wrong the error is a whole UTC offset in a direction that can permit
overspending. The hook itself never touches timezones (it reads a precomputed
epoch), so the hot path is stdlib-only on every platform.

The hook is a separate entry point (`niceclaude-hook`) targeting a module that
imports only `json`, `os`, `sys` and `time`. That is not fussiness: it runs on
every tool call in every agent and subagent, and routing it through the main CLI
module would drag in `argparse`/`re`/`subprocess` and cost ~13ms a call.
Measured: 16ms for the minimal module, 29ms via the full CLI, and 16ms for the
bash+jq implementation this replaced — the shell version was no faster, because
it spawned `jq` two or three times per invocation.

## Why the hook is installed globally, and how it is exempted

This reverses an earlier decision, so both halves are worth stating.

Hooks merge additively across settings scopes, and a narrower scope cannot
un-register one defined in a broader scope. So `install` originally wrote only a
`--settings` fragment, on the reasoning that a global install could never be
exempted, and would therefore silently pace foreground work.

The constraint is real. The conclusion was not, for two reasons:

- **The hook is already a no-op where no rule matches.** It resolves the folder
  policy first, from `policy.json` alone. That ordering exists for a different
  reason — getting it wrong made unpaced folders pay a ~2s `/usage` refresh per
  tool call whenever the snapshot was stale — but it also means a global install
  changes nothing outside the folders you explicitly named.
- **The exemption does not have to live in settings.** `NICECLAUDE_OFF` is
  per-*process*, which is strictly finer-grained than any settings file: it
  exempts one session, in one shell, wherever that session runs. Settings scope
  could never have expressed that.

What the old shape bought in exchange was steep: `niceclaude on <folder>` looked
like the whole interface but silently did nothing unless you also remembered to
launch with `--settings`. A pacer that looks installed while doing nothing is the
failure mode this tool works hardest to avoid everywhere else, and the install
story was quietly committing it. `niceclaude status` now reports whether the hook
is registered at all, so the two facts — *is this folder paced* and *is anything
positioned to pace it* — can no longer be confused.

The fragment is still written, and `claude --settings <fragment>` still works, for
anyone who wants foreground sessions to be not merely exempt but hook-free.

## Policy

Folder-scoped, with subfolders inheriting and longest-prefix winning:

```bash
niceclaude on  ~/projects/nightly --model opus
niceclaude off ~/projects/nightly/vendor     # carve out a subtree
niceclaude status .                          # which rule matched, and why
niceclaude global off                        # break glass: release everything
```

`policy.json` is re-read on every tool call, so turning pacing on or off takes
effect on an already-running agent at its next checkpoint. No restart.

### Pacing everything, and what `global` actually does

`niceclaude global off` / `global on` is a **master kill switch only**. `off`
suspends every rule at once; `on` restores them. It never *enables* pacing
anywhere — a folder is paced if and only if some rule matches it, and `global`
just gates whether those rules are consulted. It defaults to on, so `global on`
is only ever an undo for a previous `global off`.

To pace everything, pace a folder that contains it. The filesystem root is a
valid rule and works as a catch-all:

```bash
niceclaude on  / --model opus          # or `niceclaude on C:\ --model opus`
niceclaude off ~/projects/urgent       # then carve out what should run free
niceclaude on  ~ --model opus          # narrower catch-all: just your home dir
```

Longest-prefix still decides, and the root is the shallowest rule there is, so
every existing rule keeps overriding it and carve-outs work exactly as before.

Two agents in the *same* folder necessarily share a policy. Either pace the
**subdirectory** the worker runs in — longest-prefix only matches downward, so a
parent stays untouched — or start the supervisor with `NICECLAUDE_OFF=1`, which
exempts that session without touching the policy at all.

## Pacing against only some windows

Not every project should answer to every window. A project you are actively
tending wants the 5-hour line to smooth it out, but the weekly line exists to
protect budget for days you are *not* here — so it has no business throttling
work you are doing right now.

```bash
niceclaude on ~/projects/alpha --model opus --enforce session
```

`--enforce` takes any combination of `session`, `week` and `model` (the
per-model weekly bucket). The default is all three. `niceclaude status` prints
which windows a folder answers to and marks the rest `ignored`, so it never
lies about what is actually being enforced.

This is what makes pacing useful in the *foreground*: several projects worked
round-robin can each be smoothed across their 5-hour block without any of them
being held back by a weekly budget you are deliberately spending.

## Holding fan-outs to a higher bar

Spawning a dozen subagents commits to far more consumption than taking one more
step in work already under way. `--fanout-reserve` adds to `m1` for
`SubagentStart` only, so a running agent can finish while new parallel work is
held back:

```bash
niceclaude on ~/projects/nightly --model opus --fanout-reserve 10
```

Default 0, which makes the two events behave identically.

## Knowing whether it is worth running

```bash
niceclaude burn
```

Reports consumption rate per bucket and the duty cycle it implies. A sample
reading from heavy Opus work:

```
week:all models
  average rate      1.41 %/h  (idle included)
  busy-bin p90      4.00 %/h
  pace line rises   0.60 %/h
  implied duty cycle 15%  (~9 min of work per hour at this intensity)
```

The asymmetry is the useful part: the 5-hour session line rises at 20 %/h and
barely binds, while the weekly line rises at 0.60 %/h and is the real governor.
Tune the weekly margins; the session ones hardly matter.

## Running the daemon

```bash
niceclaude watch     # foreground; refuses to start twice (pidfile)
niceclaude stop      # stops it
```

`deploy/` has a systemd user unit, a container entrypoint, and a Windows
Scheduled Task script.

**Pacing still works without it.** If the snapshot goes stale the hook refreshes
on demand, costing a couple of seconds on that one tool call. What you lose is
the *record*: that refresh only fires on paced folders, at most every 180s, and
only while work is actually running — so idle time never gets sampled at all.

That biases exactly the analyses that matter. `burn` would never see the idle
stretches and would overstate your consumption rate; `plot` would show a record
that looks like continuous activity. (On a real weekend log, 87% of samples had
no live session window — with no daemon, that entire story is invisible.)

Both tools detect this and say so: `status` warns when the snapshot is older
than 180s, and `burn` reports its median sampling interval and flags input that
looks activity-driven rather than continuous. `check` is unaffected — for parser
regression, sparse real-world samples are as good as dense ones.

## Tests

```bash
uv run --with pytest pytest tests/ -q
```

145 tests, no network, no tokens, a few seconds. `tests/smoke_installed.py`
additionally exercises the installed entry points — run it after
`uv tool install .`

## Declaring the model

Hooks receive `cwd`, `session_id`, `tool_name`, and `agent_type`, but **not the
model**. So `--model` has to be declared. It decides whether the per-model
weekly bucket is enforced: Fable draws on both its own weekly budget and the
shared one, while Opus and Sonnet have no per-model bucket at all.

## Verifying the parser

Every sample stores `claude -p /usage` output verbatim alongside the parsed
fields, so a parser fix can be re-validated against the entire history:

```bash
niceclaude check
```

It asserts that usage never decreases inside a window, that reset timestamps
stay stable (within the two-minute jitter the server's rounding introduces),
and that no line went unparsed.

## Notes

- `/usage` reports whole percentages. That 1% quantum is a floor on how finely
  the pacer can act: 1% of the 5h session window is 3 minutes, 1% of the weekly
  window is 100 minutes. It supplies a deadband for free, which is why there is
  no deadband setting.
- Braking is a sleep, not a denial. Returning non-zero would hand the model a
  refusal to reason about, costing tokens and derailing the task.
- `PreToolUse` fires between API turns, so a freeze parks between connections
  rather than stalling one mid-flight.
- Hooks fire inside subagents too, so a fan-out freezes as a whole with no
  coordination between the agents.
- Claude Code invokes the hook **synchronously and blocks on it** — that
  blocking is the freeze. The hook must therefore never fork or background
  itself; if it returned early the agent would sail through and the pacer would
  look installed while doing nothing.
- The daemon writes a pidfile. Do not manage it by matching on process name:
  any shell whose command line merely mentions `niceclaude watch` matches the
  same pattern, so a name-based kill can take out its own wrapper. Use
  `niceclaude stop`.

## Windows

Verified on Windows 10 (PowerShell 5.1, Claude Code 2.1.228) — see
[`harness/windows-results.md`](harness/windows-results.md). The freeze mechanism
holds: Claude Code invokes the hook synchronously and blocks on it there too.

Three bugs surfaced on that first run, all now fixed:

- **Claude Code runs hook commands through Git Bash on Windows**, where a
  backslash is an escape character. The native path `install` used to write was
  mangled before exec, so the hook never ran and every paced folder was silently
  ungoverned. `install` now emits forward slashes, which need no escaping and
  work under both `sh` and `cmd.exe`. This constraint is load-bearing: anything
  that ever writes a hook command must keep it shell-safe.
- **`/usage` must be decoded as UTF-8 explicitly.** `text=True` alone uses the
  locale encoding — cp1252 on a US Windows install — which turned the U+00B7
  separator into `Â·` and dropped the two most important buckets.
- **The reset clause is in the timezone printed beside it, not UTC.** On a
  machine that is not set to UTC this put every reset hours early, inflating the
  elapsed fraction and raising the pace line — an error in the fail-open
  direction. Affects any non-UTC machine, not just Windows.

Two things to know rather than fix: the hook costs ~100ms per call there against
~20ms on Linux (half of it the console-script launcher), and `niceclaude stop`
leaves a stale `daemon.pid`, because `taskkill /F` cannot run the cleanup handler.

**Source convention.** U+00B7 is written as the escape `\u00b7` in code, never
as the literal character, so every Python file stays pure ASCII. This is not
fussiness either: a PowerShell `Get-Content | Set-Content` round-trip decodes
as cp1252 and re-encodes as UTF-8, which double-encodes the separator *inside
`LINE_RE`* and breaks parsing in exactly the way the bug above did — silently,
because the file still imports and still looks correct in a terminal. Only
`git diff` shows it. `tests/test_source_encoding.py` enforces the convention.
Comments keep the literal, where readability wins and corruption is harmless.

## Releasing

See [RELEASING.md](RELEASING.md). Publishing is a tag push; the workflow builds,
tests, validates metadata, and installs and smoke-tests the built wheel before
anything reaches an index. Authentication is PyPI Trusted Publishing, so there
is no API token anywhere.

## Design notes

`harness/` carries the design record — the control law and why it has that
shape, the measured platform behaviour behind each decision, what remains
unverified, and a token-free test matrix. Start with `harness/README.md`.

## License

MIT. See [LICENSE](LICENSE).
