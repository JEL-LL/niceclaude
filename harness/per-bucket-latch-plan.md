# A descent is owed to the bucket that earned it, not to every bucket

`hard` -- the running-down latch -- is one boolean for the whole verdict while
the pace lines are per bucket. A hold earned by the session crossing its brake
line is discharged against whichever banded bucket wakes LAST, which on the
weekly line is hours.

Symbols are cited by name, not line number. Test docstrings cite decisions
(L-numbers), never review-round finding numbers.

**Status: part A committed as `f93bdcd`; the docs phase (section 4a) is done.
Scope was part A only** -- the in-process fix. Publishing the latch so `status`
can show it (B) and a CLI to clear it (C) are deliberately not here; see
section 5.

Docs phase done: `README.md`, `CHANGELOG.md`, `design-decisions.md` §4a, §22
and the new §24, `test-matrix.md` cases 20-21, and this file in
`harness/README.md`.

The docs phase corrected one figure in this file: the problem statement
above read "hours to days". The discharge ceiling is one full band of weekly
line, `band * 7 * 86400 / (100 - m0 - m1)` -- **13.5h** at the shipped
margins, and 29.8h at the reporting user's wider m0 20 / m1 1 / band 14,
which is where "days" came from.

Touched: `src/niceclaude/hook.py`, `tests/test_niceclaude.py`,
`tests/test_blind_delay.py`, and E10's heading in `expired-window-plan.md`.
`_shared.py` and `cli.py` are untouched, as L7 requires: `bucket_pace`
already solved each bucket's own wake, so the fix is `decide` aiming at the
right one.

Section 3's three tests were flipped deliberately and renamed. Each new
docstring names the test it replaced, cites THE BLIND RULE's per-bucket form,
and asserts the bounded `blind-release` rather than merely "not confident".
Their mirror -- an unusable sibling beside a bucket descending through its
OWN band, which stays confident -- is pinned alongside them, as is section
4's case 8, the latched bucket's own stale band.

**Test baseline before any change:** 930 passed, 5 skipped at `da760b7`
(`uv run --with pytest pytest tests/ -q`, Linux, ~31s). **After part A: 959
passed, 5 skipped.** The 29 are 11 new tests and 18 new cells in
`test_the_blind_rule`, whose `hard` boolean axis became a three-way `latched`
axis: nothing, the hot bucket's key, the band bucket's key.

Round 1 review: no code defects. Three comment corrections and three test
corrections were applied, none of them a behaviour change. The substantive
one is what an inherited band costs -- `run` caps a soft hold at
`brake_start + band_delay`, so a band left behind by a cleared descent is
charged `max(0, band_delay - (now - brake_start))`, which is zero for any
descent longer than band_delay; the comment had claimed a full band_delay.
`test_a_crossed_bucket_runs_down_to_its_own_throttle` now pins the
running-down branch's `wake_at`, which is what makes it fail under the
"keep the `due` gate, revert `wake_at` to `banded`" mutation.

Round 2 (second-model gate): one log label, one comment correction, one new
test. A hold that armed a descent and ends on the soft cap now releases as
`band-release-after-descent` rather than `band-release`, whose named knob the
printed duration contradicted; the gate is the latch set, not `was_soft`, so
a blind hold that becomes an ordinary throttle keeps the plain label
(`test_a_descent_that_ends_on_the_soft_cap_says_so` pins the new label, and
`test_a_blind_hold_that_sees_the_band_becomes_a_throttle` the plain one).
The hot-branch jitter comment now names what actually armed the old boolean
on a jitter-zone verdict -- anything else: an earlier over-line pass, or a
hot sibling with a real wake; alone, such a verdict was blind and the old
`not soft and not blind` guard never armed on it -- verified by executing
HEAD's `decide`. The jitter zone is the fourth parametrize case of
`test_only_a_bucket_with_a_real_wake_latches`, with the hot-sibling-alone
contrast; it is the only case that dies under the "gate on saw-a-line,
dropping the wake-still-ahead half of `h[3]`" mutation. After round 2: 961
passed, 5 skipped. The new release label reached README and CHANGELOG in the
docs phase recorded above.

---

## 1. The problem

`run` sets a single `hard` on any hold that is neither soft nor blind.
`decide`'s `banded and supported` branch then discharges it against
`max(h[2] for h in banded)` -- the last-waking banded bucket, regardless of
which bucket earned the latch. `bucket_pace` already solves each bucket's
release against its OWN throttle line, so the per-bucket instants exist on
every pass; the defect is only that `decide` aims at the wrong one.

Reproduced at the shipped margins (m0 5, m1 8, band 7), session 30% over its
own line, week 50% merely inside its band:

```
t+0.0h          hold='hard' wait= 0.90h  session 30% over line 22.4%; week 50% in band
t+0.5h LATCHED  hold='hard' wait= 9.44h  session in band; week in band [running down]
t+1.0h LATCHED  hold='hard' wait= 8.94h  week:all models 50% in band [running down]
```

A 0.9h session brake becomes a ~10h hold. By t+1.0h the session is clear and
unmentioned: the agent is held solely by a weekly band it wandered into, under
a latch a different bucket earned.

### 1.1 Why it recurs, when `hard` is only a local

`hard` lives in `run()` and dies with each tool call, so this inflates ONE
call. The cycle the user reported -- "a single step every day or so, and only
an expensive one" -- needs the defect to RE-ARM, and it does, because a fresh
session window's brake line starts at `m0`:

```
session 8% on a 9-min-old window -> region=over  line=7.6%

t+  0m          wait=13.81h  session 8% over line 7.6%; week 52% ...
t+ 20m LATCHED  wait=13.47h  session in band; week in band
t+ 40m LATCHED  wait=13.14h  week:all models 52% in band [running down]
```

The loop: a long hold releases when the WEEK reaches its throttle; the 5-hour
session window expired unused during it (one window is at most 5h, the hold is
longer); the next step is cache-cold and therefore big; it opens a fresh
session window whose line starts near 5%, which that step is instantly over;
that earns a latch; the latch is discharged against the week again. One step
per hold, reliably the expensive kind.

So the latch never needed to persist to hurt. Part A breaks the loop at its
one load-bearing joint.

---

## 2. Decisions

Settled by a second model (Fable) before any code, because this is a control
law change and the orchestrator has a record of improvising this class of
decision inline.

- **L1. Latch the bucket KEY.** The literal string in `state["buckets"]` --
  `"session"`, `"week:all models"`, `"week:Fable"`. Not the pess, not the line
  height: the wake is re-solved from fresh readings every pass and never
  committed to (design-decisions §6), so the only durable fact worth
  remembering is *which bucket was seen over its own brake line during this
  invocation*.

- **L2. Only a bucket with a REAL wake latches.** `decide`'s hot branch emits
  `"latch": [h[0] for h in hot if h[3]]` -- the keys of hot entries whose
  fourth field says their wake is supported. Every other branch emits an empty
  latch. An unusable, expired, or over-the-floor-with-no-clause entry never
  SAW its brake line, so it has no descent to owe; this is the same argument
  that already excludes blind holds from latching.

- **L3. `run` accumulates, and its old guards go.** `hard = False` becomes
  `latched = set()`; the latch block becomes `latched |= set(d.get("latch")
  or ())`. The existing `not soft and not blind` guards become provably
  redundant -- a soft verdict comes only from the soft branch, which emits no
  latch, and a blind verdict has no real hot wake -- so they go, and the
  comment warning about the blind-latch trap goes with them. That trap
  (a band bucket with a future wake "justifying" a descent it never earned)
  is structurally impossible once a latch can only be discharged against its
  own bucket.

- **L4. `decide` discharges per bucket.** `hard=False` becomes
  `latched=frozenset()`. Then `due = [h for h in banded if h[0] in latched]`,
  `supported = any(h[2] > now for h in due)`, and the running-down branch
  becomes `if due and supported:` with `wake_at = max(h[2] for h in due)`.
  Other banded entries stay named in the reason for log fidelity but no longer
  lengthen the hold.

- **L5. The hot branch does not change.** `wake_at = max(h[2] for h in hot)`
  stays. Two buckets over their own brake lines must BOTH be satisfied, each
  wake already solves against its own throttle, and the max is the
  conjunction -- that is the lines doing their job, not the defect. A latched
  banded bucket beside a hot one needs no folding in either: the loop
  re-decides each chunk, and once hot clears the running-down branch takes
  over against the latched bucket's own wake.

- **L6. The hysteresis guarantee survives, per bucket.** Bucket K crosses its
  brake line; while over, the hot branch holds and the wake solves to K's
  throttle. When the line rises past `pess_K`, K reclassifies `band`, K is in
  `latched`, `due` is non-empty, and K's own wake -- the identical solve,
  re-run each pass so sibling spend that raises `pess_K` pushes it later --
  keeps the hold hard until `low_K >= pess_K`. Release happens at K's throttle
  with the full band of headroom behind it. What is removed is only the
  EXPORT of that guarantee to buckets that never crossed anything.

- **L7. `cmd_status` passes no latch and should not.** It calls `decide`
  without the argument today and keeps doing so. A one-shot command has no
  memory of where a hold began, and inventing one would make `status` disagree
  with the hook.

---

## 3. The risk, stated before the work

Three tests pin the CURRENT cross-bucket behaviour, and each must flip
deliberately:

- `test_a_running_down_hold_is_not_blinded_by_an_unusable_sibling`
  (`tests/test_blind_delay.py`)
- `test_a_stale_band_still_justifies_a_latched_descent`
  (`tests/test_blind_delay.py`)
- `test_a_latched_descent_is_not_unlatched_by_a_live_band_sibling`
  (`tests/test_niceclaude.py`)

In each, the latch is earned by the SESSION, the session then goes unusable or
expired, and the WEEK's band wake keeps the hold confident for hours.
Per bucket, `due` is empty and these become blind, released at `blind_delay`.

**That flip is correct by the project's own doctrine.** A sibling's band
reading says nothing about the earner's descent; a hold no reading can justify
is the definition of blind; and §22 ships `blind_delay` as exactly this bound,
under the user's rule that it is better to fail free than stall forever. The
doctrine case that matters survives: a STALE reading of the latched bucket
itself still supports its own descent, because a stale pct is a lower bound
and its wake is finite. "Stale data may justify braking" is intact wherever it
is honest.

**The failure mode to guard against** is an implementer or a review round
"preserving" those tests by letting any banded wake re-support any latch,
which silently reintroduces the whole defect behind the new per-bucket
plumbing. The flips must be made on purpose, with docstrings rewritten to cite
THE BLIND RULE's new per-bucket form.

`test_a_latched_descent_whose_window_expires_goes_blind_not_free`
(`tests/test_niceclaude.py`) should still pass, under the new signature.
`expired-window-plan.md` E10's heading -- "the `hard` latch needs no change"
-- becomes false and must be corrected there.

---

## 4. Tests

1. The section 1 repro: session over its line, week merely banded. The hold is
   the session's own crossing, and does not jump to the week's throttle when
   the session drops into its band.
2. The section 1.1 repro: a fresh session window at `m0`, the cold-step case.
   The descent is bounded by the session window, not the week.
3. L6 per bucket: a bucket that crossed its own brake line still runs down to
   its OWN throttle with the full band behind it, not to the brake line.
4. L2: an unusable, an expired, and an over-the-floor-no-clause hot entry each
   fail to latch.
5. L5: two buckets both over their own lines still wait for the later of the
   two.
6. The crossing itself, not a single instant -- walk time across the
   over-to-band transition and assert the wake target never changes bucket.
   A single-instant test is blind to this whole class.
7. The three flips in section 3, each asserting the bounded blind release
   rather than merely "not confident".
8. A latched bucket's own STALE reading still supports its own descent.

---

## 4a. What the docs phase owes

Part A is a user-visible behaviour change and `README.md`, `CHANGELOG.md` and
`design-decisions.md` are deliberately not in it, on the split the
`blind-delay` and `expired-window` work both used. Enumerated by the
second-model gate plus one found while checking it, so the next phase derives
its scope from the change rather than from memory:

- **The headline.** Holds are materially shorter: a session brake no longer
  discharges against the weekly throttle. That is the entry a user deciding
  whether to upgrade actually needs.
- **`hook.log` reads differently, in two ways.** A `brake` now commonly ends
  on the band cap (see the label chosen for the gate's finding 1), and the
  three flipped cases end `blind-release` where the old code held confident
  for hours. README's hook.log reasons section needs both.
- **README's region table is now wrong.** It states the band costs "one
  `band_delay` per tool call". That is true only of a hold soft from the
  start. A band inherited at the end of a hard descent costs
  `max(0, band_delay - (now - brake_start))`, which is ZERO for any descent
  longer than `band_delay` -- and inheriting a trailing band was rare before
  this change and is now the common ending. This is the same false claim the
  round-1 review corrected inside `decide`'s comment (C1); it lives in the
  user-facing table too, and the correction did not reach it.
- **`status` now commonly disagrees with a mid-descent hook.** L7 is
  deliberate -- a one-shot command has no memory of where a hold began -- but
  it stops being an edge case once descents are per bucket, and stays that way
  until section 5's B ships.

---

## 5. Not in scope, and why

The user asked for two further things and approved A alone first:

- **B. Publish the latch so `status` can show which bands are descending.**
- **C. A CLI command to set or clear a latch**, so a hold descending to the
  weekly throttle can be dropped to the band-delay behaviour while the user is
  at the machine and wants to talk to the model before leaving.

Both need the latch to live outside the hook process, and the design is
already half-settled: one sidecar file keyed by RULE PATH, mirroring
`policy.json`'s shape (which is itself one file keyed by path, not a file per
folder). Scope it per rule rather than per agent: usage and time are shared
across an account, so every agent is sleeping at the same moment, and what
differs between rules is `m0`/`m1`/`band` -- which is exactly what moves a
throttle line. Two agents in one folder share a rule and should share a latch.

Put the sidecar beside `state.json` in the per-account `DATA_DIR`, not beside
`policy.json` at `ROOT_DIR`: latches derive from usage, and usage is
per-account, while `policy.json` is deliberately account-global (§19).

C needs no new IPC. A frozen hook re-reads policy every chunk -- that is how
`global off` frees a held agent mid-hold -- so a clear command writes and the
sleeping agent picks it up within one chunk.

Do A first and observe. If long holds persist afterwards, `hook.log` will say
why: a reason naming `week:all models NN% over line` is the weekly governor
working as configured, not this defect, and the remedy there is configuration
(`--enforce session`, §17) or C -- not a control-law change that shortens a
hold earned over a line.
