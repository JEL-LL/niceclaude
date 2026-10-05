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

### 4a. …but the deadband is on the wrong side (amended)

The section above is still right about what it actually argues: **no deadband
above the line.** Letting usage run some margin `B` over the brake line before
braking would spend the ceiling to buy smoothness, and the 1% quantum already
supplies more hysteresis than an explicit `B` would.

What it got wrong is treating that quantum as symmetric. The quantum bounds
the *entry* to a brake. It says nothing about the *exit*, and the exit is where
the cost was hiding:

> a hold ends when `allowed(t) ≥ pess`, and `pess = pct + 1`

So a brake releases at the moment the line reaches one quantum above the
reported number — which means you resume with **under one quantum of real
headroom**, and the very next 1% tick puts you over again. The claimed deadband
is consumed entirely by the release, leaving none for the run that follows.

That turns the weekly line into the worst possible duty cycle: a 1.68-hour hold,
then roughly 1% of budget spent, then another 1.68-hour hold. Every hold is far
longer than the prompt-cache TTL, so **every 1% of weekly budget costs one full
cold context re-read**. §4's closing claim — "1.68-hour weekly brakes follow work
bursts measured in hours, so one re-payment amortises over a long run" — assumed
the burst between brakes was long. It is one quantum wide, which is exactly as
long as the quantum is, and that is not long at all.

**The fix is hysteresis, and it lives entirely below the brake line.** A second
line sits `band` percent lower, and a hold aims at *that* one. The brake line
keeps its meaning untouched — never above it — so §4's argument survives intact:
we still do not let usage run over the line before braking. We let it come
further *down* before releasing, which is a different knob on a different side
and costs nothing from the ceiling.

One hold then buys a whole band of running instead of one quantum, and the cold
re-read amortises over the band rather than over the tick. Setting `band_delay`
additionally turns that band into a lower gear rather than a free sprint, so the
brake line is approached slowly and often not at all.

The relationship to `max_delay` is the part worth keeping straight. `max_delay`
also bought cache-warmth, but it paid for it out of the ceiling — it is the one
knob that proceeds while over the line, which is why it is opt-in and why it is
how you end up hitting your head. The band buys the same warmth out of headroom
you had not yet spent. They compose, and they no longer compete.

**Hysteresis needs memory, and `decide` has none.** This is the part that is
easy to get wrong, and it was wrong in the first draft — where the band tested
green, read correctly in every docstring, and did nothing at all.

`decide` is stateless by design: it reclassifies from scratch on every pass, so
one snapshot can serve many folders and a policy edit reaches a frozen agent
within one chunk. But that means a bucket held above the brake line *stops being
`over`* the instant the line rises past `pess` — which is precisely the old,
zero-headroom release point. Recomputed honestly, the hold then ends there:
`region` becomes `band`, nothing brakes, and the log says `line-caught-up`,
which is also what a correct release says. The feature evaporates and leaves no
trace that it did.

`bucket_pace` returning the right wake time is not enough, because nothing ever
gets to use it. The memory has to live in `run`, which latches "this hold began
above the brake line" and passes it back into `decide`. A hold that began above
the line runs all the way down; a hold that wandered into the band from below is
a lower gear and costs one `band_delay`. Same region, opposite answers — and the
only thing that can tell them apart is where the hold started.

The testing lesson generalises: every test of a controller that samples a single
instant, or starts the loop already in its steady state, is blind to this entire
class of bug. The crossing is the behaviour.

**Ceiling worth knowing:** `install` registers the hook at `timeout`, and that
is the real limit on any hold — past it the harness kills the hook and the agent
proceeds *unpaced*, silently, since a killed process never logs its release.

It was 21600 (6h), and that leaked measurably: see `platform-findings.md` §8 for
the 198 unmatched brakes, spaced six hours apart to the second, in a folder that
had explicitly asked for no cap at all. Now 172800 (48h), which clears every
hold observed in practice. A hold is bounded by its window's own reset, so the
true worst case is a single window — seven days — and 604800 would put the
ceiling out of reach entirely.

This still interacts with band sizing, just no longer dangerously: a band costs
its width in hold time, ~1.94h per point on the weekly line. Keep it small
because a hold is time not working, not because the harness will cut it short.

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

## 8. Install globally; exempt with an environment variable, not a settings scope

**Reversed.** This decision originally read *"install as a `--settings`
fragment, never globally"*, and the reasoning below is why — followed by why it
was wrong.

Hook settings merge **additively** across scopes (enterprise → user → project →
local → `--settings`), and **a narrower scope cannot un-register a hook defined
in a broader one.** So the conclusion drawn was that installing into
`~/.claude/settings.json` would pace foreground work with no way to exempt it.
`niceclaude install` wrote a standalone fragment, and only background invocations
passed `--settings <that file>`; foreground sessions didn't have the hook
disabled, they didn't have it at all.

The premise about settings scopes is correct and still is. Two things make the
conclusion not follow:

1. **The exemption need not be a settings scope.** `NICECLAUDE_OFF` is
   per-process. That is *finer*-grained than any settings file can be: it
   exempts one session in one shell, which is precisely the case this decision
   originally called impossible. It is also what lets an unpaced supervisor share
   a folder with a paced worker — a job decision 7's longest-prefix matching
   previously had to carry alone, via a subdirectory rule.
2. **A global hook is already inert where no rule matches.** `paced_entry()`
   resolves policy before touching the snapshot — added for a different reason
   (getting the order backwards made unpaced folders pay a ~2s refresh per call
   whenever the snapshot was stale), but it means the blast radius of a global
   install is exactly the set of folders named in `policy.json`.

What tipped it was the cost on the other side. `niceclaude on <folder>` reads as
the entire interface, and under the fragment-only design it silently did nothing
unless you also remembered `--settings` on every launch. That is the tool's own
worst failure mode — *looks paced, isn't* — the one decision 11's fail-open-and-
log rule and the Windows backslash fix (decision 7's closing note) both exist to
keep out of the hot path. Shipping it in the install story was inconsistent.

Consequences, all of which the merge has to earn:

- `install` **merges** into a file it does not own, so it preserves unrelated
  keys and other people's hooks, refuses a file it cannot parse rather than
  overwriting it, and updates in place on reinstall rather than registering a
  duplicate (two entries double per-call latency, and after the tool venv moves
  one of them is dead while the file still looks right).
- `uninstall` exists, because a merging install needs a way back out that is not
  hand-editing JSON. `tests/test_install_merge.py` asserts the round trip
  restores the original exactly, which is the only assertion strong enough to
  catch an incidental mutation at depth.
- `status` reports whether the hook is registered *at all*, so *is this folder
  paced* and *is anything positioned to pace it* stop being confusable — the
  confusion that prompted the reversal.
- The fragment is still written. Someone who wants foreground sessions hook-free
  rather than merely exempt loses nothing.

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

Still true of the payload, but no longer the only way: §20 lets a folder
declare `detect` and read the model per call from the caller's transcript.

---

## 11. Fail-safe direction

- Unparseable/missing snapshot → **brake** (the tool exists to prevent overspend).
- Crash inside the hook → **fail open** and log. A bug must never wedge every
  session; problems should surface via `check` and `hook.log`.
- A long hold → **keep holding.** There is no self-imposed ceiling. `MAX_BRAKE`
  (6h) used to be one, justified as "by then every window has rolled" — false,
  since the weekly and per-model weekly windows run seven days and are precisely
  the ones that bind for days. It also never fired once in 388 logged releases,
  because `install` registers the hook at `timeout: 21600`, the identical number,
  and the harness clock starts ~0.2s earlier at spawn. Two ceilings remain, and
  both are deliberate: `max_delay`, which you set per folder, and the registered
  timeout, which is the harness's. A release *while blind* is still logged
  distinctly, because it is the one release we cannot justify from data.
- The registered timeout kills the hook **silently** — the process dies before it
  can log. So an unmatched `brake` in `hook.log` means "timed out", not "hung".
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

---

## 18. `tzdata` on Windows, to buy out an inference that could fail open

`/usage` prints reset times in the zone named beside them, and on an ordinary
workstation that is an IANA name like `America/New_York`. Reading them as UTC
placed every reset four hours early on such a machine, which inflates the
elapsed fraction, which raises the pace line, which permits spending that should
have braked. Fail-open — see `windows-results.md` for the measurement.

Fixing the parse required resolving the zone, and there `zoneinfo` splits by
platform: Linux and macOS have a system tz database, **Windows ships none**.
Without one, `ZoneInfo("America/New_York")` raises and the only remaining option
is to read the time as machine-local.

That fallback is correct **if** the renderer prints the machine's own zone. Two
machines agreed and it is the obvious implementation, but it is not a documented
contract, and when such an inference is wrong the error is a whole UTC offset
whose direction depends on the sign — so it can land fail-open.

**`tzdata` is declared as a Windows-only dependency to remove the inference.**
Verified: with no system database `ZoneInfo` raises `ZoneInfoNotFoundError`;
with `tzdata` present it resolves.

The cost is bounded precisely:

- The marker `sys_platform == 'win32'` keeps Linux and macOS dependency-free.
- `tzdata` is pure data, no code, maintained by the Python core team.
- **The hot path is untouched.** The hook never parses a timezone; it reads the
  epoch the daemon already computed, so it remains stdlib-only everywhere.

"Zero dependencies" was a nice property, but it is not worth holding an
unverified assumption on the one axis where being wrong permits overspending.
The local-time fallback is retained as a backstop for installations that skip
dependencies, and is documented as such rather than as the intended path.

---

## 19. One data directory per Claude account, one policy for all of them

One machine can run several Claude accounts, by launching sessions with
`CLAUDE_CONFIG_DIR=~/.claude-work claude`. niceclaude used to have one data
directory whatever the account, so every account read and wrote the same
`state.json`. Each sample was correct; what went wrong was that nothing said
whose it was. A hook in account B paced on account A's snapshot whenever it
was fresh, and a `watch` started in A kept it fresh forever, so B was paced
on A's numbers almost entirely. That fails in both directions, which is worse
than most bugs here: where A is well under its line, B spends straight past
its own while looking paced. The histories mixed too, so `burn`, `plot` and
`check` differenced across unrelated series.

The fix keys the directory by account **and** stamps each snapshot and record
with the account, because neither alone is enough: the path cannot separate
two accounts that share a directory, and the stamp alone turns two busy
accounts into steady refresh churn over one file. The full reasoning, the
options rejected and seven rounds of review are in `per-account-state-plan.md`;
these are the decisions, under the labels that plan and the code use.

- **D1. `policy.json` is shared across accounts.** Policy is about folders,
  usage is about accounts, and §9 already splits the two. A per-account policy
  would bring back *looks paced, isn't*: run `on` from a shell without the work
  account's environment and that account's hooks never see the rule. The cost
  is that one folder cannot be paced differently per account. `install
  --force` resets the shared file, so it names the other accounts it affects.
- **D2. Unset, empty, or default `CLAUDE_CONFIG_DIR` is the default account,**
  whose files stay where they always were. "Default" means it resolves to
  `<home>/.claude`, so someone who exports `CLAUDE_CONFIG_DIR=~/.claude` keeps
  their history. With the variable set, even to the default dir, Claude reads
  `$CLAUDE_CONFIG_DIR/.claude.json` instead of `~/.claude.json`
  (`platform-findings.md` §15). The login and the usage are the same, so the
  data dir is too; only the diagnostic identity (D8) sees the difference. The
  comparison does a cheap `normcase(normpath())` first, and caches the
  resolved default per home.
- **D3. `NICECLAUDE_DIR` wins outright and is never slugged.** An explicit data
  dir means exactly that directory, for every account. It keeps the old
  one-directory workaround, the test suite's redirection, and §16's one bind
  mount. Accounts sharing it are told apart by the stamp (D6) and the log
  filter (D11), not by the path.
- **D4. A config dir is normalized by `norm_config_dir`,** which is
  `normcase(normpath(realpath(value)))` and deliberately never expands `~`.
  Claude does not expand it either: given `~/.claude` it made `<cwd>/~/.claude`
  and started logged out (`platform-findings.md` §15). The key has to follow
  Claude, because no stamp can catch niceclaude and Claude disagreeing about
  which directory is meant. A relative value resolves against the cwd, as
  Claude's does; `status` warns about it.
- **D5. The slug is a readable basename plus the CRC32 of the full key,** as in
  `accounts/claude-work-1a2b3c4d`. `zlib`, imported lazily and only for a
  non-default key, never `hashlib`, whose cold import measured 9–37ms against
  a 16ms hook. A collision only puts two accounts in one directory, where the
  stamp turns it into churn rather than mis-pacing.
- **D6. Every snapshot and log record carries `config_key`,** and
  `hook.load_state` rejects a snapshot stamped by another account as `{}`. Not
  as stale: a stale snapshot is a lower bound on usage (§12), but another
  account's usage says nothing about ours, and `decide(degraded=True)` would
  brake with full confidence on it. With `{}` the hook refreshes, and if that
  fails it brakes blind, which is the honest answer. A missing or unreadable
  file is absent, not foreign.
- **D7. A missing stamp matches the default key only.** What old code wrote
  stays valid for a single-account user, and a non-default account pays one
  refresh to replace it.
- **D8. The account identity is diagnostic, and read only by the CLI.** It is
  the `accountUuid` and `organizationUuid` pair from Claude's global config,
  stamped as `account` so that `check` can note a `/login` to a different
  account inside one log, or two directories holding one login. The hook never
  reads that file: it is tens of KB and grows with project history. Email,
  names, tokens and credentials are never read, stored or printed.
- **D9. Upgrade and downgrade: stop every daemon first; nothing is migrated
  automatically.** A pre-upgrade daemon started under a work
  `CLAUDE_CONFIG_DIR` keeps publishing unstamped snapshots into the root,
  which the default account trusts under D7. `status` warns when it sees the
  signs of one. A single non-default account's old history is left in the
  root, because whether a log is one account's or two accounts' interleaved is
  something only its owner knows, and a mixed log can never be split again.
  `status` prints the two `mv`s that move it, and leaves the choice.
  Downgrade needs nothing: old code ignores the new fields.
- **D10. The config dir may also come from the hook payload — deferred.** If a
  hook can lose `CLAUDE_CONFIG_DIR` (`claude --config-dir`, or
  `CLAUDE_CODE_SUBPROCESS_ENV_SCRUB=1`), the hook could recover the config dir
  from `transcript_path`. Nothing yet shows that it can: the check needs a
  second login, and is filed as Procedure B in `open-questions.md` §10. Until
  it runs, a hook that loses `CLAUDE_CONFIG_DIR` is keyed as the default
  account.
- **D11. `load_log` drops only records stamped by another account.** An
  unstamped record is kept under every key. That is looser than D7 on
  purpose: wrongly trusting a snapshot mis-paces, and it is transient, while
  wrongly hiding history cannot be undone. A directory two accounts shared
  before the upgrade stays mixed for that span, as it already was.

Two consequences shape the commands:

- **`install` is per account, and records the account.** It writes into the
  `settings.json` of the config dir `CLAUDE_CONFIG_DIR` names, so an account
  nobody ran `install` from has no hook at all. It records each config dir in
  `accounts.json` at the root, and `uninstall` marks the entry off rather than
  dropping it. That registry is the only place a config dir is known from,
  since a slug is one-way. `status` lists every account it knows of, checks
  each one's hook live in its own `settings.json`, and says whether its daemon
  is running. The registry records what `install` and `uninstall` did, not
  whether a hook exists: one can also come from project settings or the
  `--settings` fragment.
- **One daemon per account.** Each account has its own pidfile, so `watch` and
  `stop` act on the account their own environment names. One daemon sampling
  every account was considered and dropped: it would sample serially, so one
  hung `claude` would stall every account, and it would poll accounts whose
  credentials had expired. `deploy/` has a templated systemd unit and a
  `-ConfigDir` for the Windows task instead.

---

## 20. `--model detect`: pace each call on its caller's own model

§10's one declared model is wrong for a session that runs on one model and
spawns subagents on another. Declared `opus`, an organizer's Fable subagents
never answer to `week:Fable`. Declared `fable`, the organizer answers to it
too, and once that bucket is spent every call lags, Opus included. No payload
carries the model, but every caller has a transcript, and the `message.model`
of its newest assistant record says which model wrote it. `detect_model`
ports the user's proof of concept (`open-questions.md` §9) to pure Python;
`model_family` maps the id onto the word `model_matches` compares
(`claude-fable-5-1` gives `fable`). The full plan, the Phase 0 measurements
and the smaller decisions (D3–D8) are in `model-detection-plan.md`, under the
labels used here and in the code.

The only per-model bucket ever recorded is `week:Fable`, so in practice this
decides one thing per call: pace on the Fable line or not. Nothing here grows
machinery beyond that.

- **D1. `detect` is a value of `--model`, not a precedence rule.** A declared
  model behaves exactly as before, and `decide` ignores `caller_model` under
  it. `--model detect`, stored as `"model": "detect"` (`DETECT`, matched
  case-insensitively), makes `decide` use the family `run` detected instead.
  With no `--model`, no per-model bucket is enforced, as before. When
  detection finds nothing, the call has no per-model bucket, and `session`
  and `week` still apply.

  Rejected: a precedence rule, with the detected model winning and the
  declared one as the fallback (the likely answer `open-questions.md` §9
  recorded before the user chose `detect`, as Q1 in the plan). The user chose
  `detect` as one more value of `--model` (plan D1). It is the simpler form:
  there is no precedence to resolve. Two things follow from that: a declared
  folder never opens a transcript, and an undetected call is never paced on a
  declared model that, in a mixed session, would be the wrong one.
- **D2. `SubagentStart` does not detect.** The subagent's transcript does not
  exist yet when it fires (`platform-findings.md` §16, every run).
  `detect_model` returns None for it before looking, skipping both the
  derived path and the search. The search would fail on every fan-out and
  walk the whole project directory each time: 17 ms over 9,750 files warm,
  and one project dir here holds 22,541. Here the port departs from the proof
  of concept, which searches and prints `unknown`. It also skips
  `<synthetic>` records, skips a torn last line that would abort the proof
  of concept's `jq`, and stops at 8 MiB (plan D4, §2 and D6).
  The cost is bounded: a Fable subagent launches past the Fable line, and its
  first tool call, which sees its own record in every Phase 0 run, is held on
  it. `--fanout-reserve` therefore does not reach `week:Fable` under
  `detect`.
- **D9. A missing per-model bucket is ignored** (the user's
  call, made in Phase 1 review). When `model` is the only window enforced and
  the caller matches no row, `decide` returns `region: "free"` rather than
  the hard blind brake. That covers an Opus call, an unread model and a
  `SubagentStart` alike. In the user's words: "If you are just supposed to
  hold on Fable and you don't have Fable information, you don't just stop
  and catch fire, you just run free." Without it, every such call in an
  `--enforce model` folder took "no usable buckets in snapshot", uncapped
  under the default `max_delay`, and froze the organizer until the harness
  timeout. The case it lets through: a Fable call whose `week:Fable` row is
  missing, because the renderer drops a row whose utilization is null, runs
  unpaced. The row was present in 35,330 of 35,330 samples.

  Rejected:
  - *a list of the families that have buckets*, so that only a known Fable
    caller with no row would brake. It fails safe, but it needs upkeep every
    time a per-model bucket appears;
  - *a blind hold, as under a declared model.* It freezes the organizer, for
    hours, on a bucket it never draws on.

  The early return is narrow on purpose. An empty snapshot still brakes
  blind, and so does one missing an enforced `session` or `week` bucket
  (the cp1252 misparse once left only `week:Fable`): that is "cannot see",
  not "nothing applies", and §11 and §17 still govern it.

  The rule does not depend on `detect`. It was first built for `detect`
  only, then extended to declared models at the user's request ("fix §12 the
  same way"). So `{"model": "opus", "enforce": ["model"]}`, which names a
  model with no bucket, runs free too, where it used to brake blind on every
  call (`open-questions.md` §12).

Detection is lazy and cheap. `wants_detection` gates it to a paced `detect`
rule that enforces `model`, after `paced_entry`, so an unpaced folder never
opens a transcript. `run` tries it at most once per invocation, because a
frozen agent cannot change model while frozen. The backwards read
(`_last_model`) gives up after `DETECT_CAP` (8 MiB) and returns None, and
`detect_model` never raises: an exception would fail the hook open.
`model_tag` adds `model=<family>(detected|declared)` or `model=none` to
`brake` and `throttle` lines in `hook.log`, so a hold can be explained.
