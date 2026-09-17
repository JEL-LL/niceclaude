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

`niceclaude version` prints the installed version, read from the distribution's
metadata rather than from a literal in the source, so it reports what is actually
installed. Be aware of what it cannot tell you: for a git install the number does
not move between commits either, so two builds many commits apart both report
the same thing. To tell whether the *code* is current, check that a command you
expect is present (`niceclaude --help`) and that the plumbing is in place
(`niceclaude status .` distinguishes "registered" from "fragment only").

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

Every command has a full page. `niceclaude help on` (or `niceclaude on --help`)
says what the command does and what it reads and writes, describes each argument
with its default, and gives examples. `niceclaude help` alone lists the commands.

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

## Capping a single hold, so the prompt cache survives it

Being far over the line can solve to a wait of hours. That is correct as
restraint and can be self-defeating in practice: a hold longer than the prompt
cache TTL means the next turn re-reads the whole context from cold, so a wait
taken to save budget can cost more than it saved.

`--max-delay` caps **one hold**, not the total restraint:

```bash
niceclaude on ~/projects/nightly --model opus --max-delay 240   # seconds
```

Over the line, the hook now holds four minutes, releases while *still* over,
lets the agent take one step, and brakes again at the next `PreToolUse`. The
restraint is still applied — just as many short holds rather than one long one,
and the duty cycle it produces is roughly the same. What changes is that no
single wait outlives the cache.

Default is no limit, which holds until the line catches up — or, where a band is
configured, until the throttle line below it does. Pick a value under your cache
TTL; note the TTL drops sharply once an account is on overage billing, so a cap
chosen for the normal case may not help there.

To remove a cap:

```bash
niceclaude on ~/projects/nightly --no-max-delay
```

That writes an explicit `null` rather than dropping the key, so it also
overrides a cap set in `defaults` — otherwise "off" would silently leave one in
force wherever a default is configured.

This is the one setting that deliberately proceeds while over the line, so it
is opt-in. `niceclaude status` reports the cap and what it will actually do:

```
right now:      BRAKED -- session 90% over line 48.5%
                holds 4m00s (max_delay), then proceeds while still over
                the line; the line itself clears in 2h27m (Tue 17:46 local).
                Each later tool call brakes again for up to 4m00s.
```

Releases from a cap are logged as `max_delay-release` — distinct from
`line-caught-up`, so `hook.log` never conflates "we waited it out" with "we gave
up waiting".

It is no longer the only answer to the cache problem, though, and it is no
longer the first one to reach for. A cap buys cache warmth out of the *ceiling*,
because proceeding while over the line is exactly what it does. The next section
buys the same warmth out of headroom you had not spent yet.

## A second line below the first, so one hold buys more than one tick

A hold used to end the moment the line reached `pess = pct + 1` — the
pessimistic reading of a number quoted in whole percent. That releases the agent
with under one quantum of real headroom, so the very next 1% tick puts it over
again. On the weekly window that is a 1.68-hour hold for every 1% of budget
spent, and every one of those holds is far longer than the prompt cache TTL. The
quantum supplies a deadband on the way *in* to a brake and none at all on the
way out, so the steady state was: hold, re-read the whole context from cold,
spend 1%, hold again.

`--band` puts a second line below the pace line and makes a hold run down to
*that* one:

```
brake line      allowed = m0 + f_t * (100 - m0 - m1)      # unchanged
throttle line   low     = max(m0, allowed - band)
```

The brake line keeps its exact old meaning — never above it — so nothing here is
spent from the ceiling; the hysteresis lives entirely below the guarantee. The
`max(m0, ...)` floor is what keeps a fresh window startable. `m0` is the
grubstake that lets work begin at all, and without the floor a window that had
just rolled would throttle its very first step, which is the one step the
grubstake exists to permit. With it, the band opens as the window advances.

Three regions, judged against `pess`:

| region | when | what happens |
|---|---|---|
| free | `pess <= low` | full speed, no hold |
| band | `low < pess <= allowed` | one `band_delay` per tool call |
| over | `pess > allowed` | hold until the **throttle** line catches up |

Both braking regions aim at the same target, the throttle line; the brake line
only decides whether the hold is capped. That is where the money is. One hold
now buys a whole band of running instead of one quantum, so the cold re-read
amortises over the band rather than over the tick.

### The two knobs are orthogonal

`--band` is geometry and nothing else. It is a percentage, it defaults to 0, and
0 is not a special case that switches a feature off: it puts the throttle line
on top of the brake line, which is exactly the previous behaviour. There is no
`--no-band` because `--band 0` already says it.

`--band-delay` is what one hold costs *inside* the band. Left unset — the
default — the band is pure release hysteresis: you run through it at full speed,
and only the brake line ever stops you. Set it, and the band becomes a lower
gear, one hold per tool call taken while still under the pace line, so the brake
line is approached slowly and often not reached at all.

`--max-delay` is unchanged and keeps its old meaning, which makes four corners
worth knowing. Over the brake line with no cap, the hook holds until the
throttle line catches up; with a cap it holds that long and then proceeds while
still over the line, as before. Inside the band with no `--band-delay`, nothing
holds at all; with one, a hold costs `band_delay`, or `min(band_delay,
max_delay)` where the cap is the smaller. The two are not alternatives — the
tighter one wins.

Read against each other, they differ in what they spend. `--max-delay` buys
cache warmth out of the ceiling: it is the one knob that deliberately proceeds
while over the line, which is how you end up hitting your head. `--band` buys
the same warmth out of headroom you had not yet spent. They compose, and they no
longer compete — so if you set a cap to protect the cache, set a band first and
see whether you still want the cap.

One thing `--band-delay` will not do is release a *blind* agent. A snapshot too
stale to trust is reported as over the brake line, because "I cannot see" must
not resolve into "I am comfortably under the line". Only `--max-delay` proceeds
on data we know we do not have.

### Sizing a band, and the ceiling that limits it

`install` registers the hook with `timeout: 172800` — 48 hours, and that is the
real limit on any hold. Past it the harness kills the hook, and a killed hook
does not brake: the agent proceeds *unpaced*, silently, because the killed
process never gets to log its release. An unmatched `brake` in `hook.log` is the
only trace.

It was six hours, which leaked: a weekly bucket can easily solve to a hold
longer than that, so a folder far over its line took one unpaced step every six
hours, indefinitely. Note the shape of that bug — it is worst exactly where you
asked for the most restraint, since `max_delay: null` means "hold however long
it takes" and the timeout was silently rewriting it to "hold six hours, then go
anyway".

A hold is bounded by its window's own reset, so the longest one that can ever be
demanded is a single window — seven days for the weekly buckets. 48h covers
everything seen in practice with room to spare. If you want the ceiling provably
unreachable rather than merely generous, 604800 is the number.

Do not remove the field to lift the limit: omitted, it reverts to the hook
default of ten minutes, which is far worse than the ceiling you were trying to
escape.

The weekly line rises about 0.52 %/h at the default margins, so sizing a band
is no longer constrained by this — but a band still costs its width in hold
time, so keep it small for the reasons in the section above.

To size one against a run you already have rather than by arithmetic:

```bash
niceclaude plot --days 7 --band 2
```

With no geometry flags at all, `plot` draws the line the **current folder is
actually paced against**: the `m0`, `m1` and `band` of the rule covering it,
falling back to `defaults` for anything that rule does not set, resolved
exactly the way `status` resolves them. A bare `niceclaude plot` therefore
grades a run against the line that governed it rather than against the
built-in 5/8/0, which is what it used to do — a folder paced with a band saw
its band missing from its own chart. The line it took is printed before it
draws:

```
  line: m0=5, m1=8, band=2  (from rule /home/you/projects/nightly)
```

Where no rule covers the folder it says so and uses the policy defaults, and
any flag you pass is named on that line too, so you can always tell which
numbers were yours.

`plot`'s `--band` — like its `--m0` and `--m1` — overrides that default and is
drawing only. It shades the band onto the log you already recorded and changes
no policy, so you can try a number against last week before pacing anything
with it. Time in the shaded
strip is time that would have been spent in the lower gear, cache-warm and still
under the guarantee. Too thin and the trace keeps punching through to the brake
line; too fat and it never leaves the band.

### The two configurations, and what `status` shows

Pure hysteresis — brake less often, and run at full speed once released:

```bash
niceclaude on ~/projects/nightly --model opus --band 2
```

The full lower gear — the same hysteresis, plus a deliberate crawl between the
lines:

```bash
niceclaude on ~/projects/nightly --model opus --band 2 --band-delay 30
```

`--no-band-delay` goes back to full speed through the band. Like `--no-max-delay`
it writes an explicit `null`, so it also overrides a `band_delay` set in
`defaults`.

`niceclaude status` reports the middle region as a state of its own, because
calling it BRAKED would say work has stopped when it has not, and calling it
running would hide a hold on every tool call:

```
  session                 20% used | lines  46.5/ 48.5% |  50.0% elapsed | ENFORCED | clear
  week:all models         47% used | lines  46.5/ 48.5% |  50.0% elapsed | ENFORCED | THROTTLES 30s/call

right now:      THROTTLED -- week:all models 47% in band (line 48.5%, throttle 46.5%)
                holds 30s per tool call, then takes one step: a lower
                gear, not a stop, and still under the brake line.
                Full speed resumes in 2h54m (Wed 21:03 local), when the
                throttle line catches up.
```

Both lines are printed once a band is configured, throttle first: one number
cannot say which of three regions a bucket is in, and the throttle line is the
one a hold actually runs down to, so quoting only the brake line would make
every wait shown above look longer than the hook will take.

Band holds are logged with the verb `throttle` rather than `brake`, so they
neither swamp `hook.log` nor spoil the invariant that an unmatched `brake` means
the harness killed the hook — `grep ' brake '` still counts real brakes.

## Why it is stopped, and for how long

A frozen agent looks identical to a hung one. `niceclaude status .`, run against
the folder it is sitting in, prices every line and then says what the hook would
do right now:

```
  session                 60% used | line  31.1% |  30.0% elapsed | ENFORCED | HOLDS 1h44m
  week:all models         31% used | line  18.9% |  16.0% elapsed | ENFORCED | HOLDS 1d02h
  week:Fable               3% used | line  18.9% |  16.0% elapsed | ignored  | clear

right now:      BRAKED -- session 60% over line 31.1%; week:all models 31% over line 18.9%
                releases in 1d02h (Sat 14:45 local); next check in 15s
                That release is not a promise: every session draws on
                the same account-wide budget, so it can move out.
```

Every line is priced, including the ones this folder ignores. Which line is the
painful one is not obvious in advance — the weekly line rises at 0.60 %/h against
the session line at 20 %/h, so their waits differ by orders of magnitude — and an
`ignored` row reading in days is the argument for or against your `--enforce`
set. Where a `--fanout-reserve` is configured, the stricter wait a
`SubagentStart` faces is reported too.

Two of those numbers are different things. **releases in** is when the line
rises to meet current usage. **next check in** is the sleep chunk: a braked
agent re-reads policy and re-derives its release that often, which is both why
`niceclaude global off` frees it within 15s and why the release time can move
out while it waits.

The verdict line comes from the hook's own `decide()` rather than a second copy
of the arithmetic, so `status` cannot report `running` while the hook is holding
the agent. When the snapshot is too stale to trust, it says `BRAKED, blind` and
prints no release at all, because there is none to solve for.

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

388 tests, no network, no tokens, a few seconds. `tests/smoke_installed.py`
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
  window is 100 minutes. It supplies a deadband above the line for free, which
  is why there is no setting for one — but it supplies none below the line,
  where the release happens, which is what `--band` is for.
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
is no API token anywhere. [CHANGELOG.md](CHANGELOG.md) records what changed in
each version; the version itself is stored only in `pyproject.toml`.

## Design notes

`harness/` carries the design record — the control law and why it has that
shape, the measured platform behaviour behind each decision, what remains
unverified, and a token-free test matrix. Start with `harness/README.md`.

## License

MIT. See [LICENSE](LICENSE).
