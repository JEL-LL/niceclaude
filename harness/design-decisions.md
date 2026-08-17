# Design decisions

Each entry records what was decided, why, and what was rejected. Where a
decision was reached by measurement rather than reasoning, the number is in
`platform-findings.md`.

---

## 1. Goal: pace, don't merely survive

Three different problems hide behind "stop Claude hitting its limits":

1. **Don't get cut off mid-task.** Reactive detection is enough — catch the
   limit error, sleep until reset. Small.
2. **Pace a budget** so unattended work doesn't consume what you need later in
   the week. Needs real accounting and a policy. Different program.
3. **Unattended endurance** across multiple windows.

This tool is (2), with (3) as a consequence. That matters because **pausing
never creates capacity** — with rolling windows, deferring work moves it in
time, it does not raise the ceiling. Only deliberate self-throttling changes
outcomes.

---

## 2. The pace line

For each window: if you are `f_t` of the way through its *time*, you should be
at most `f_t` of the way through its *budget*.

```
allowed(f_t) = m0 + f_t * (100 - m0 - m1)
```

- `m0` (default 5) is a **starting grubstake**. The pure diagonal permits 0% at
  0% elapsed, so without `m0` nothing could ever begin.
- `m1` (default 8) is an **end-of-window reserve**, so you come in under the
  wire rather than exactly on it. It also absorbs the quantization error in §4.

**Why this shape and not a schedule.** It needs no day-of-week policy, no
weekend handling, no holiday logic. Skip a day and headroom accumulates on its
own; hammer the foreground and background work goes quiet until the line catches
up. It self-balances against whatever actually happens.

**Rejected: a fixed daily allowance** (1/7 of the week per day). It requires
modelling your calendar, handles unexpected absence badly, and wastes headroom
that a quiet day should have banked.

---

## 3. Braking is a sleep, not a denial

A hook can stop a tool call two ways. Only one is right.

- **Deny** (exit 2): control returns to the model with a message. The model then
  *reasons about* the refusal — apologises, tries a workaround, burns tokens,
  derails the task.
- **Sleep**: the hook process simply doesn't return. Claude Code blocks on it.
  The agent freezes in place holding its full context, and on release continues
  as though nothing happened.

**Sleep, always.** Denial spends the very budget the tool exists to conserve.

**Corollary the implementation depends on:** Claude Code invokes hooks
synchronously and blocks. That blocking *is* the freeze. The hook must never
fork or background itself — if it returned early the agent would sail straight
through and the pacer would look installed while doing nothing.

---

## 4. No deadband — the signal already has one

The obvious worry is stutter: brake the instant you cross the line, release,
immediately cross again, and pay the prompt-cache re-creation cost every cycle.
The intuitive fix is a deadband: let usage run some margin `B` over the line
before braking.

**Not needed.** `/usage` reports **whole percentages**. That 1% quantum is a
floor on the minimum detectable overage, hence on the minimum possible brake:

| Window | 1% of window | Minimum brake |
|---|---|---|
| Session (5h) | 3 minutes | 3 min |
| Weekly (168h) | 100.8 minutes | 1.68 hours |

The quantization *is* a deadband, and on the weekly line it is already enormous.
An explicit `B` would only make an already-coarse controller coarser.

**This holds independent of burn rate.** Pause length = overage ÷ line-rise
rate. Overage is floored at 1% by the quantum; the rise rate is fixed by the
window length. Burn rate changes how long the *work burst* is, never how long
the *pause* is — so the conclusion survives contention between sibling agents.

It also disposes of the cache concern in both directions: 3-minute session
brakes sit under the prompt-cache TTL so the cache survives, and 1.68-hour
weekly brakes follow work bursts measured in hours, so one re-payment amortises
over a long run.

**Consequence:** the weekly loop is chunky. Under observed load the weekly
number moves ~1% every two hours, so you get a new observation about every two
hours and each control action lasts ~1.68 hours. It is "decide whether to run,
every couple of hours", not a fine-grained throttle. Expect that rather than be
surprised by it.

---

## 5. Round pessimistically

Because reported `P%` could really be anything up to `(P+1)%`, the hook compares
`pct + 1` against the line. This is quantization error folded into the same
margin `m1` already exists to provide.

---

## 6. The wake time is computed, but never committed to

Usage never falls inside a window, so the moment the line rises to meet current
consumption is solvable in closed form.

**But the hook must not sleep straight to it.** Usage is frozen only for *this*
agent — the foreground session and sibling agents draw on the same
account-global budget and can consume during the freeze. An agent that slept
blindly to a precomputed instant would wake into a *worse* position than it went
under, and immediately blow the line.

So tier 1 sleeps in bounded chunks (`chunk`, default 15s), re-reads the snapshot
and policy, and recomputes. The wake time floats with real conditions.

`chunk` is also how long a policy change takes to reach a frozen agent, so it
trades responsiveness against idle wakeups. It only runs while something is
actually frozen.

---

## 7. Policy is keyed on folder, not session

Hooks receive `cwd`, `session_id`, `transcript_path`, `tool_name`, and
(inside subagents) `agent_id`/`agent_type`.

Folder-keying was chosen because it matches how people think — a project is a
directory — and because subfolders inherit naturally via longest-prefix match.

It also turned out to be **more robust than session-keying**: whatever
`session_id` a subagent reports, its `cwd` is the parent's. Keying on folder is
therefore immune to any future divergence in subagent session identity. (As
measured, `session_id` is currently uniform across the tree too, so either would
work today.)

Matching is on **path components, not string prefix** — otherwise `/foo/bar`
matches `/foo/barbaz` and silently paces the wrong tree. Paths are normalized
with `normcase` + `normpath` + `realpath`, which matters on Windows where
`C:\Proj` and `c:\proj` are the same directory but would otherwise be different
policy keys.

**One path is not ours to normalize: the hook command in `settings.json`.**
Claude Code runs hook commands through a shell, and on Windows that shell is
**Git Bash**, where a backslash is an escape character. A native path like
`C:\Users\me\.local\bin\niceclaude-hook.exe` is silently eaten before exec — no
error, no log, and every paced folder runs completely ungoverned while still
reporting itself paced. `cmd_install` therefore writes forward slashes on
Windows; they need no escaping and are accepted by the Windows API under both
`sh` and `cmd.exe`. Quoting still covers spaces, and is orthogonal.

Verified by a hook command of `echo x > /c/tmp/marker` landing at
`C:\tmp\marker`, and by a mangled backslash path producing a file literally
named `C<U+F03A>ncworkM-user.txt`. See `windows-results.md`.

Two normalization limits found on Windows and left as-is: `realpath` resolves a
`subst` drive back to its target (so `Z:\proj` and `C:\proj` share a policy,
which is right), but it does **not** resolve a UNC path to its local equivalent,
so `\\server\share\proj` and `C:\proj` are separate policy keys.

**Known limitation:** two agents running in the *same* folder cannot have
different policies. This surfaced when setting up a supervisor/worker pair —
an unpaced supervisor babysitting a paced worker — where both would naturally
run in the same repo.

The workaround uses the inheritance rule rather than fighting it: pace the
**subdirectory** the worker runs in and leave the parent unmatched. Longest
prefix only matches downward, so a supervisor in `/repo` is untouched by a rule
on `/repo/project`. A git worktree gives the same separation with a full copy
if the worker genuinely needs the repo root.

If per-session policy is ever actually needed, `session_id` is available in the
hook payload and `--session-id` lets a launcher choose it in advance — but that
is a real complexity increase and folder-keying has covered every case so far.

---

## 8. Install as a `--settings` fragment, never globally

Hook settings merge **additively** across scopes (enterprise → user → project →
local → `--settings`), and **a narrower scope cannot un-register a hook defined
in a broader one.**

So installing into `~/.claude/settings.json` would pace foreground work with no
way to exempt it. Instead `niceclaude install` writes a standalone fragment, and
only background invocations pass `--settings <that file>`. Foreground sessions
don't have the hook disabled — they don't have it at all.

---

## 9. Two tiers, and the snapshot carries no decision

The daemon publishes *raw usage*; the hook computes *decisions*.

Putting decisions in the snapshot would require one snapshot per folder, since
`m0`/`m1`/`model` vary per policy. Publishing raw means one snapshot serves
every folder.

The hook self-heals: if the snapshot is older than 180s it calls
`niceclaude refresh` synchronously. That costs a couple of seconds on one tool
call but stops a dead daemon from either wedging every agent or silently letting
them run unpaced.

---

## 10. Model must be declared

No hook event carries the model — verified across `PreToolUse`,
`SubagentStart`, `SubagentStop`, and `UserPromptSubmit`. So `--model` on the
folder policy is required, not a convenience.

It decides whether the per-model weekly bucket is enforced. Fable has its own
weekly budget **and** draws on the shared one; Opus and Sonnet have no per-model
bucket at all. Enforcing an unmatched model's bucket would brake on a budget the
agent isn't spending.

Enforced buckets: `session`, `week:all models`, plus `week:<declared model>` if
such a bucket exists. Brake on whichever demands the latest wake.

**Matching the label needs whole-word comparison, not equality.** The renderer
produces per-model labels two different ways: a hardcoded `"Current week
(Sonnet only)"` for max/team subscriptions, and a server-supplied `displayName`
for model-scoped limits (the source of the observed `"(Fable)"`). So the label
is neither stable nor predictable, and `week:sonnet` never equals `week:sonnet
only`.

An equality check therefore **fails silently** — the per-model weekly bucket
simply goes unenforced, with no error and nothing in the log, which is the worst
possible failure for a budget guard. `model_matches()` splits the label into
words instead, which handles both known forms and any future display name
containing the model's name. See `platform-findings.md` §4.

---

## 11. Fail-safe direction

- Unparseable/missing snapshot → **brake** (the tool exists to prevent overspend).
- Crash inside the hook → **fail open** and log. A bug must never wedge every
  session; problems should surface via `check` and `hook.log`.
- Braked longer than `MAX_BRAKE` (6h) → release and log loudly. By then every
  window has rolled, so continuing to hold means something is wrong with us, not
  with the budget. A timeout release *while blind* is logged distinctly, because
  it is the one release we cannot justify from data.
- **Exception — a freshly rolled window.** After a reset the server omits the
  reset clause entirely (`Current session: 0% used`), so `f_t` is unknown. Naive
  fail-safe would brake *hardest at the moment headroom is greatest*. Instead,
  judge against `m0`, which `allowed()` never dips below. See
  `platform-findings.md` §3.

---

## 12. Stale data is a lower bound, not noise

The failure that matters most: the hook wakes, tries to refresh, and **the
refresh itself fails** — network down, auth expired, or (the case this tool is
built around) usage already exhausted and `/usage` behaving unexpectedly.

An early version silently proceeded on whatever was in `state.json`, treating an
hours-old snapshot as current. That is the worst option: an agent could run all
night deciding from data taken before it went to sleep.

The resolution comes from monotonicity. **Usage only ever rises within a
window**, so an old reading is a *lower bound* on current consumption. That
gives an asymmetric rule:

> Stale data can justify **braking**, but never **allowing**.

Concretely:

- Degraded snapshot already over the line → **brake with full confidence.** It
  can only have got worse since.
- Degraded snapshot under the line → **brake anyway**, flagged `BLIND`. Being
  under the line according to data known to be out of date is not evidence of
  headroom.
- Fresh snapshot under the line → allow.

The two brake reasons are logged distinctly (`BLIND: snapshot Ns old and refresh
failing` versus `session 60% over line 22.4%`), and a mid-brake transition
between them is logged too. "Stopped because over budget" and "stopped because
blind" demand completely different responses, and `hook.log` is the only
overnight record.

Refresh attempts back off — 0, 15, 30, 60, 120, 300s — so a failing endpoint is
not hammered. But the *wake* cadence stays at `chunk`, because policy is re-read
every cycle: the kill switch must free a blind agent within one chunk rather
than one backoff interval.

---

## 13. Pure Python, stdlib only

Originally bash + `jq`, on the assumption that a shell hot path would beat
interpreter startup. **Measurement showed it did not** — the shell version spawned
`jq` two or three times per invocation, and each `jq` start cost about a whole
Python start. Both landed at 16ms.

Python therefore won on portability at zero latency cost, and made a whole class
of bug impossible: jq's `//` operator falls back on `false` as well as `null`,
which silently disabled the kill switch and would have turned a configured
`m0: 0` into `5`. `dict.get` has no such trap.

The hook lives in its own module importing only `json`/`os`/`sys`/`time`, with
`subprocess` imported lazily. Routing it through the CLI module would drag in
`argparse`/`re`/`subprocess` and cost ~13ms on every tool call in every agent
and subagent.

---

## 14. Daemon lifecycle via pidfile, never process-name matching

`pgrep -f "niceclaude watch"` matches *any* shell whose command line mentions
that string — including the wrapper script trying to do the killing. This
killed the controlling shell twice during development. The daemon writes a
pidfile; use `niceclaude stop`.

**The pidfile needs an explicit signal handler.** Cleanup lives in a `finally:`,
but Python's default `SIGTERM` handler terminates *without unwinding*, so the
`finally` never ran — every `stop`, `systemctl stop` and `docker stop` left a
stale pidfile behind. `read_pid()` liveness-checks, so this was usually
invisible; the failure appears only when a **recycled PID** matches the stale
entry and the next `watch` refuses to start. That is unlikely on a workstation
and quite likely in a fresh container PID namespace with a bind-mounted data
dir — i.e. exactly the deployment this tool is for.

Fixed by installing handlers for `SIGTERM`/`SIGINT`/`SIGHUP` that raise
`SystemExit`, which unwinds normally. Verified before and after: the old build
left `STALE: 3257`, the fixed build leaves nothing.

Two related sharp edges, left as-is but worth knowing:

- `watch` **exits 1** when a daemon is already running. Any supervisor with a
  restart policy needs a start-limit guard or it will spin. The shipped systemd
  unit sets `StartLimitIntervalSec`/`StartLimitBurst` for this.
- `stop` returns 0 whether or not it stopped anything, so its exit status
  cannot detect failure. Idempotent-stop is the right semantic, but a
  supervisor wanting certainty should signal the PID directly.


---

## 15. A fan-out is held to a stricter line than a tool call

`SubagentStart` originally ran the identical check as `PreToolUse`, which is
defensible — it does at least hold the spawn — but it treats two very different
commitments the same way.

Taking one more step in work already under way costs one turn. Spawning a
fan-out commits to a dozen agents each running their own tool loop. When
headroom is thin, the right behaviour is to let the running agent finish while
refusing to start new parallel work.

`fanout_reserve` adds to `m1` for `SubagentStart` events only. It changes the
bar, not the control law, so nothing about the pace line becomes
event-dependent. Default 0, which preserves the previous behaviour exactly.

```bash
niceclaude on ~/projects/nightly --model opus --fanout-reserve 10
```

Verified with a single folder at a single instant: `PreToolUse` allowed while
`SubagentStart` braked. Brake reasons carry the event name so the log
distinguishes them.

The hook learns the event from `hook_event_name` in the payload, which is
present on every event type.

---

## 16. Config and data directories are separate — and only one had an override

`settings.json` lives in `CONFIG_DIR` (`~/.config/niceclaude`,
`%APPDATA%\niceclaude`) while `usage.jsonl`, `state.json`, `policy.json` and
`daemon.pid` live in `DATA_DIR` (`~/.local/share/niceclaude`,
`%LOCALAPPDATA%\niceclaude`). That split follows platform convention and is
fine on a workstation.

It was a trap for containers: `NICECLAUDE_DIR` relocated only the data dir, so
persisting state needed **two** bind mounts, and forgetting the second one
loses `settings.json` — which silently unpaces everything rather than failing
loudly.

`NICECLAUDE_DIR` now also relocates config (to `<dir>/config`) unless
`NICECLAUDE_CONFIG_DIR` is set explicitly. One mount is now sufficient, and the
override remains available for anyone who wants the directories apart.

---

## 17. A folder chooses which windows it answers to

The windows are not interchangeable. They exist for different reasons:

- The **5-hour window** smooths a burst. It rises at 20 %/h, close to the rate
  heavy work consumes it, so it mostly stops you sprinting rather than stopping
  you working (`platform-findings.md`, and the duty-cycle table in
  `open-questions.md` §3).
- The **weekly window** protects budget for days you are not at the machine. It
  rises at 0.60 %/h and is the real governor.

Applying both to everything conflates those purposes. Work you are actively
tending has no reason to answer to a line whose whole job is to reserve budget
for your absence — you are *there*, spending it deliberately.

`enforce` selects any combination of `session`, `week`, and `model`; the default
is all three, preserving prior behaviour. `--enforce session` is the
round-robin foreground case: several projects each smoothed across their own
5-hour block, none of them throttled by a weekly budget being spent on purpose.

Two deliberate choices:

- **A malformed or empty value enforces everything.** This tool restrains
  spending, so an unparseable config must not silently un-pace a folder that
  still reports itself as paced. Failing toward restraint is the only safe
  direction.
- **Choosing a window that is absent from the snapshot brakes as `blind`**, it
  does not wave the agent through. If you asked to be paced against the session
  window and no session bucket is reported, the honest answer is "cannot tell",
  and the fail-safe applies (§11, §12).

`niceclaude status` prints the enforced set and marks the others `ignored`, so
the display can never disagree with the hook about what is being enforced.
