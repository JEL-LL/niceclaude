# A reading whose window has rolled is void, not a position

When a snapshot's window has reset since the reading was taken, the reading
describes a window that no longer exists. It is neither a lower bound on
current usage (the window was zeroed) nor evidence of headroom (the new window
is unobserved). `bucket_pace` treats it as a position anyway and extrapolates
its pace lines off the end of the window.

Symbols are cited by name, not line number.

**Status: done. Phase 1 (behaviour) -- `_shared.py`, `hook.py`,
`cli.py`, `plot.py`, `tests/test_niceclaude.py`, plus a one-line key-set
update in `tests/test_band.py` (see below) and a new sighting 18 in
`open-questions.md`. Suite green at 930 passed, 4 skipped (baseline 873).
Review rounds: 3 under `pipelines/ratchet.md` -- 13, 12 and 17 findings, all
applied -- then a second-model gate. Phase 2 (docs) done: `README.md`,
`CHANGELOG.md`, `design-decisions.md` §11, §12 and the new §23,
`test-matrix.md` cases 17-19, this file in `harness/README.md`, a new failure
mode 9 in `pipelines/ratchet.md`, a new sighting 21 (and a corrected heading
on 19) in `open-questions.md`, and -- committed code edited during the docs
phase, because section 1.2's figure had propagated into it --
`src/niceclaude/_shared.py`'s comment in `bucket_pace`. Both ran under
`pipelines/ratchet.md`.**

This change's review history is not recorded here. Its one durable lesson --
that wrong RATIONALES outnumbered wrong code roughly two to one, and that the
mitigation is to demand every written claim be demonstrated -- is a property
of the pipeline, not of this feature, and lives in
`pipelines/ratchet.md` failure mode 9.

The rounds changed behaviour in six places beyond the original plan, all
inside Phase 1's blast radius.

- `decide`'s soft-band return replaces `wake_at` with `now + chunk` when, and
  only when, the band wake is already behind us -- mirroring the hot arm's
  `real`. A band wake IS in the past throughout the jitter zone, which `hot`
  guarded and `banded` did not; an unconditional floor (round 1's first
  attempt) was wrong in the other direction, rounding ordinary near-future
  crossings up to a whole chunk.
- `bucket_pace` returns the no-reset-clause shape for a window that has not
  BEGUN -- a clause further out than one window length. Clamping its lines
  alone was not enough and was actively worse: the solve still produced a
  far-future wake that `decide` read as real, making an 18h hold CONFIDENT
  and so unreachable by `blind_delay`, while the clamped lines hid how broken
  the reading was.
- `decide` reports `expired` on every paced verdict, not only the hot one.
- `cmd_status`'s "conditional, not a verdict" caveat is gated on
  `age > MAX_STALE or d.get("expired")`, because E6's refresh force fires
  regardless of age.
- A corrupt `state.json` can no longer crash the hot path (E13). A
  non-positive `window_seconds` raised `ZeroDivisionError` into `hook.main`'s
  fail-open -- section 1.4's hazard reached directly, and the only defect here
  that silently UNPACED a folder rather than mis-timing it.

  **The gate then found that the first version of this guard did not close
  the class its own comment claimed to close.** `start = resets - window` was
  evaluated in the ternary *ahead* of the `window <= 0` test, so non-numeric
  corruption still raised: `window_seconds "18000"` and `resets_epoch "x"` at
  the subtraction, `pct "50"` at `pess = pct + 1`. Only numeric garbage was
  ever safe. Three same-model rounds read that comment and agreed with it.
  Now a `placed` type test runs before ALL arithmetic: non-numeric position
  fields take the E13 floor shape, a non-numeric `pct` returns `None` (the
  function's already-documented "unusable" contract, which `decide` files as
  a wake-less hot entry and THE BLIND RULE classifies blind). A JSON `true`
  still passes as a number, because `bool` is an `int`; that is sighting 15
  and is deliberately left open.
- `cmd_status` no longer promises a crossing that cannot happen, nor calls a
  future window start "unknown" (E11, E12). Both were false sentences the
  earlier fixes introduced, which is why E11-E14 were settled by a second
  model rather than improvised in a fix dispatch.

One deviation from section 3's file list, flagged rather than hidden: E3's
"the expired shape carries exactly the same key set as a live shape" (test
10) requires `expired` on EVERY shape `bucket_pace` returns, not only the
voided one. `tests/test_band.py`'s
`test_every_return_shape_carries_the_region_and_both_lines` pins that key
set exactly, so it needed the new key added -- one line, in the test that
guards the same property.

**Test baseline before any change:** 873 passed, 4 skipped at `3a282a3`
(`uv run --with pytest pytest tests/ -q`, Linux, ~32s).

---

## 1. The problem

With the daemon stopped for ~52h, `niceclaude status` printed:

```
  session                  3% used | lines 839.9/853.9% | 1055.5% elapsed | ENFORCED | clear
  week:all models         44% used | lines  84.7/ 98.7% |  99.7% elapsed | ENFORCED | clear
```

`bucket_pace` computes `elapsed = (now - start) / window` with no upper bound.
For a 5h window read 186925s ago, `elapsed` is 10.56, so the brake line
`m0 + elapsed * span` reaches 853.9% and the throttle line 839.9%. Reproduced:

```
session  pct=3  allowed=853.8  low=839.8  elapsed=10.6  region=free
week     pct=44 allowed= 98.8  low= 84.8  elapsed= 1.0  region=free
```

The week bucket is fine -- it is genuinely at 99.7% of its window. Only the
session window has expired.

### 1.1 The verdict was right by accident

`status` printed `BRAKED, blind`, which is correct. But it is correct only
because both buckets classified `free` -- the lines had floated above any
plausible pct -- and `free` plus `degraded` falls through to the blind branch
in `decide`. Nothing in that path reasoned about the expired window at all.

### 1.2 Clamping `elapsed` to 1.0 is a regression

The obvious fix makes the display sane and the braking wrong. A stale 95%
session reading would then sit permanently in `band`/`over` against the brake
line's final height of 92% (`100 - m1`) and brake on a window that reset days
ago -- a reading that bounds nothing. The extrapolation is currently the only
thing preventing that. Any fix must void the reading, not re-anchor it.

(That figure read **99%** until Phase 2's review recomputed it. 99 is the
final height at the reporting user's margins, m0 20 / m1 1; at the shipped
defaults it is 92. This is the third figure in this plan computed at those
margins and presented as general -- see 1.3's pct, corrected the same way --
and the first to propagate out of this file into a code comment and from
there into `design-decisions.md` §23. Three review rounds and a gate read
past it. Write `100 - m1`, not a number, wherever the number is not the
point.)

### 1.3 A live defect just past the boundary

An expired bucket slides `band` -> `free` as the line climbs past it. At the
**shipped** margins (m0 5, m1 8, band 7) the band window is pct 87-93; the
table below uses 90. (An earlier draft of this section used pct 95, which is
in band only at the reporting user's own margins of m0 20 / m1 1 / band 14 --
at the defaults 95 is already `over`. The defect class is identical either
way; only the pct that exhibits it moves.)

```
reset   30s ago, pct=90 -> region=band, wait=0.0, wake in the PAST
reset  600s ago, pct=90 -> region=band, wait=0.0, wake in the PAST
reset 3600s ago, pct=90 -> region=free
```

`decide`'s over-the-line arm guards this (`real = p["wake"] > now`) and so does
`describe_hold` (its `now` parameter). The soft-band branch does not. So a
just-rolled window with a lagging reset clause takes a `soft` hold whose
`wake_at` is in the past, and in `run` a soft hold's cap is `band_delay`:
`deadline = brake_start + cap`. The hold therefore runs the **full band_delay**
(180s by default) per tool call, napping the 1s floor throughout, until the
line climbs clear of the reading (`pess <= allowed - band`) -- about 21
minutes of wall clock after a session roll at pct 90 on the shipped margins,
and about 42 at the reporting user's.

This is reachable **without** `degraded`: a sample 100s old with a roll 30s ago
is inside `MAX_STALE` (180s), so nothing flips to degraded and nothing voids
the reading. The first-pass `NEAR_STALE` refresh usually cures it; a failing
refresh does not.

### 1.4 The hook fails open

`hook.main` catches every exception and logs `-- failing open`. A partial
return shape from `bucket_pace` that crashes a forgotten consumer converts
"this reading is void" into "this folder is not paced at all" -- worse than
either braking or running free. This constrains the design more than anything
else here: see E3.

---

## 2. Decisions

- **E1. The expiry contract.** A bucket is expired iff
  `now > resets + RESET_JITTER`. `RESET_JITTER` (120s) moves to `_shared.py`
  and `cli.py` and `plot.py` import it from there. It currently exists as two
  independent copies, which is the same disease `bucket_pace` was centralised
  to cure.

  The slack is not padding. The server rounds displayed reset times -- the
  existing comment says 8:09pm and 8:10pm are the same instant -- so a *live*
  window in its final minute can read as already past its reset.

  **What the slack buys, corrected.** An earlier draft of this decision
  justified it by the over-the-line case: that voiding a genuine 95-100%
  reading would hand it to `blind_delay`, which releases, while it was really
  over the line. That is **false**, and review caught it. Such a reading is
  blind on *both* sides of the boundary -- inside the zone its wake solves to
  `resets`, which is behind `now`, so `decide`'s `real` guard already makes
  `seen` False and the hold blind. Measured:

  ```
  pct=95 reset   1s ago -> hard blind=True  expired=False
  pct=95 reset 119s ago -> hard blind=True  expired=False
  pct=95 reset 121s ago -> hard blind=True  expired=True
  ```

  The slack's real payoff is the `free` and `band` cases: without it, every
  normal window end would spend two minutes converting ordinary under-the-line
  readings into blind hard holds. That -- plus `_unit_pace` (E5) -- is the
  justification. The constant and the strict `>` were right; only the reason
  given for them was wrong. Anyone retuning this constant should read the
  corrected reason, not the original one.

- **E2. Clamp rather than extrapolate, at both ends.** For
  `resets < now <= resets + RESET_JITTER`, clamp `elapsed` to 1.0. "The line is
  at its final height" is true for that minute; "the line is at 103%" never is.
  Combined with E1 this means **`elapsed` never exceeds 1.0 anywhere**, which is
  what retires the 1055.5% display and the `band`/`free` slide in 1.3.

  The low end needs the same clamp, which the first draft missed and test
  review found. A reset clause further out than one whole window -- clock skew,
  or a reparse landing a session reset 6-10h ahead -- drives `elapsed`
  *negative*: `now = resets - 6h` gives `elapsed -0.2, allowed -12.4`, and
  `resets - 10h` gives `elapsed -1.0, allowed -82.0`. So:
  `elapsed = max(0.0, min(1.0, (now - start) / window))`.

  **SUPERSEDED -- that formula is not what shipped, and must not be
  "restored".** The correction further down this decision (a pre-start clause
  returns the floor shape outright) branches out *before* the division, which
  makes the low clamp unreachable. Shipped code is `min(1.0, ...)` alone, and
  `bucket_pace`'s comment records why writing the `max` back would be worse
  than useless: it would read as the fix for the negative case, which it is
  not. The second-model gate caught that this decision stated a formula it had
  since contradicted and never retracted -- exactly how a future implementer
  gets misled by a document meant to prevent that.

  A clause placing `now` before its window's start is as untrustworthy as one
  past its end, but unlike expiry it needs **no new state**. The `m0` floor is
  already this codebase's answer to "no usable window position", and clamping
  to 0 lands exactly there. Giving it its own void state would be surface for
  no evidence: a not-yet-started window is precisely what `m0` exists for.

  **Clamping the lines is not enough, and the first version of this decision
  was dangerous.** Round 2 found it. Clamping `elapsed` while leaving the
  `wake` solve on unclamped geometry returns a far-future instant instead of
  `None`, and a wake in the future makes the hot entry *supported*, so the
  hold is **confident rather than blind**: `blind_delay` cannot release it and
  `max_delay` is off by default. Measured, 5h window, clause 20h out, pct 50:

  ```
  bucket_pace: region=over elapsed=0.0 allowed=5.0 wake-now=18.05h
  decide:      blind=False wake_at-now=18.05h reason='session 50% over line 5.0%'
  control (no reset clause): blind=True wake_at-now=15s
                             reason='session 50% over floor 5%'
  ```

  An 18-hour hold bounded only by `HOOK_TIMEOUT` is exactly the stall-forever
  the user's rule forbids (`blind-delay-plan.md` section 1: "I would rather
  fail free than stall for forever"). And the clamp *removed the symptom* --
  the row used to read `over line -256.0%` at `-300.0% elapsed`, obviously
  broken, and now reads `over line 5.0%` at `0.0% elapsed`, indistinguishable
  from a legitimate start-of-window brake.

  So a pre-start clause returns the **no-reset-clause shape outright**: both
  lines at `m0`, and `wait`/`wake` **None**. THE BLIND RULE then bounds it
  with `blind_delay` and it fails free. Clamping the lines without clamping
  the solve is the trap; the shape has to match on the field that decides
  blind-versus-confident, not merely on the two that are printed.

- **E3. The expired shape is total and hold-safe.** Because of 1.4, the expired
  return must carry **every key the live shape carries**, with numbers where a
  consumer will format numbers. It mirrors the existing no-reset-clause branch
  -- `allowed` and `low` both at the `m0` floor, `elapsed: None` -- and adds:

  | key | value | why |
  |---|---|---|
  | `expired` | `True` | the new state, checked explicitly by every consumer |
  | `region` | `"over"` | a forgetful consumer must hold, not proceed |
  | `over` | `True` | same, for consumers reading `over` rather than `region` |
  | `wait` / `wake` | `None` | there is no line to solve for, so no release |
  | `elapsed` | `None` | no window is in progress; `_pace` already guards this |
  | `allowed` / `low` | `m0` | numeric so a forgetful formatter cannot crash |

  `region: "over"` with `wake: None` lands it in `hot` as a wake-less entry,
  which THE BLIND RULE already classifies as blind. No new policy: "cannot see"
  is the state the codebase already answers with a blind hard hold, both for an
  empty snapshot and for a degraded under-the-line reading. An expired reading
  is strictly less informative than a degraded one.

  `allowed`/`low` at `m0` are a crash guard, **not** a line that was in force.
  No consumer may present them as one; `status` branches on `expired` first.

- **E4. `decide` gets an explicit expired arm with its own reason string.**
  Placed first in the per-bucket loop, before the `free` and `wake is None`
  arms, so it does not depend on E3's forced region. Without it the existing
  wake-less arm logs `"session 95% over floor 20%"`, which is false -- and
  `hook.log` is read by eye. Reason text: `"<key>: window reset since this
  reading"`.

- **E5. `_unit_pace` is immune under E1, and gets a test saying so.**
  `_draw_overlay_band` samples `ft` over `[0, 1.0]` inclusive, so `_unit_pace`
  calls `bucket_pace` with `now == resets_epoch == 1.0` on a synthetic
  one-second window. Under a bare `now >= resets` boundary that is expired, and
  `cmd_plot` would either draw the band plunging to `m0` at its right edge or
  raise. Under E1 it is not expired, so it takes E2's clamp and the band draws
  at its true final height.

  **What makes it immune, precisely.** The **slack alone**, not the strict
  `>`. The comparison is `1.0 > 1.0 + 120`, which is false under `>` and
  equally false under `>=`; 120s of slack swamps a one-second synthetic window
  entirely. An earlier draft of E1 claimed the strict `>` "matters on its own
  account" here -- review caught that, and it is the third wrong rationale
  this plan has carried. The strict `>` does independent work at exactly one
  place: `now == resets + RESET_JITTER`. Attribute it there and nowhere else.

- **E6. An expired bucket must force one refresh.** An expired reading voids
  *content* without aging the *file*, so the freshness gate in `run` cannot see
  it: `limit` is `NEAR_STALE` (15s) only when the region is already `band` or
  `over`, and a snapshot younger than that is never re-read. A snapshot <=15s
  old straddling the roll therefore stalls at the exact moment a fresh window
  became available -- a full `blind_delay` (240s) where a ~2s refresh would
  have cured it.

  **One case, not two.** An earlier draft claimed this also cures a first-pass
  refresh that fails while the snapshot is still inside `MAX_STALE`. It does
  not, and review caught it: if a first-pass refresh ran at all then the normal
  gate is what ran it (`limit` is already `NEAR_STALE`, because an expired
  bucket forces `region == "over"`), and once it has failed `first` is `False`,
  so the force is dead on every later pass. That case still waits for
  `age > MAX_STALE`, and should -- chasing a failed refresh is exactly what the
  `next_try` backoff exists to prevent.

  `decide` therefore reports `expired: True` on the verdict when any **enforced**
  bucket is expired, and `run`'s gate becomes
  `(age is None or age > limit or (first and d.get("expired")))`. Gated on
  `first` deliberately: a successful refresh resets `next_try` to 0, so an
  ungated force would re-run a ~2s subprocess every chunk for as long as the
  window stayed expired. One refresh per invocation is the existing guarantee
  and remains the right one.

- **E7. The existing past-wake guards stay, and they are not redundant.**
  `decide`'s `real = p["wake"] > now` and `describe_hold`'s `now` parameter
  keep doing real work after this change. An earlier draft of this decision
  claimed they became belt-and-braces once `bucket_pace` stopped extrapolating;
  that is **wrong**, and the implementation corrected it. `bucket_pace` still
  publishes a past wake *inside the jitter zone*: for
  `resets < now <= resets + RESET_JITTER`, E2's clamp puts `elapsed` at 1.0, so
  every braking bucket's wake solves to `resets`, which is behind `now`. Both
  guards still fire there. Keep them, and comment them for what they actually
  do rather than as a historical courtesy.

- **E8. Rejected: roll the window forward.** Treat an expired bucket as a new
  window at `resets` with 0% used. Sound when the snapshot is fresh, unsound
  when it is stale, so it would need gating on `degraded` -- more policy
  surface for a case already covered. It is also factually wrong: a session
  window starts at first activity and does not tile, so after 52h idle there is
  likely no live window to roll into, and `resets + k*window` fabricates one.
  The real representation of a fresh window is the post-roll sample's **missing
  reset clause**, which `bucket_pace` already judges against the `m0` floor and
  which `run`'s refresh reaches in ~2s under E6.

- **E9. Expiry is per bucket, and a live sibling still governs.** With a
  52h-stale snapshot the weekly buckets have **not** expired and remain valid
  lower bounds. An expired session alongside a week bucket that is genuinely
  over the line must still produce a *confident* (non-blind) hold --
  `seen = any(h[3] for h in hot) or supported` is satisfied by the week
  entry's real wake. The expired sibling must not drag it blind.

- **E10. The latch does not rescue an expired descent.** A latched descent
  whose window resets goes expired -> `hot` -> blind. Blind never unlatches
  (a key is only ever added to the latch, never removed, and only by the hot
  branch), and `due` counts only `banded` wakes, which an expired bucket no
  longer produces. Verified by reading, pinned by a test.

  (Written when the latch was a single boolean `hard` and the scan was
  `supported` over every banded entry. `per-bucket-latch-plan.md` made it a
  set of bucket keys and narrowed the scan to `due`, the latched subset. The
  decision survived that change untouched -- which is the point of recording
  it -- but the symbols it named did not, and a citation that does not
  resolve is the defect class this plan spent four rounds on.)

Added during review. These four were settled by a second model (Fable) after
the orchestrator twice introduced defects by improvising this class of
decision inline. They governed code that review had already written, so they
are recorded here rather than in code comments alone -- comments are where
the three wrong rationales this plan has already retracted were living.

- **E11. `wake_at` stays a nap target; `status` consults the bucket's own
  wake.** `run` naps `min(chunk, max(1.0, wake_at - now))`, so a past
  `wake_at` makes it spin the 1s floor -- which is why the soft-band return
  floors it. But `cmd_status` read that same field and printed "Full speed
  resumes in 15s ... when the throttle line catches up", false twice over in
  the jitter zone: the throttle line is not what releases the hold
  (`band_delay` is), and 15s is a synthetic nap target, not a release.

  Rejected: a separate `release_at` on the verdict. `wake_at` is *already* a
  nap target rather than a release promise in four other arms -- unusable,
  expired, no-buckets and degraded all publish `now + chunk` -- so promoting
  it would rewrite its contract against four counterexamples, and E3's
  totality doctrine would force the new field onto every shape. `cmd_status`
  already computes `bucket_pace` per row, so it holds the true wake in hand.
  It guards `band_wake is not None and band_wake <= now`, the same idiom as
  `decide`'s hot-arm `real` and `describe_hold`'s `now`.

- **E12. A window that has not BEGUN shares the floor branch and splits only
  the tail.** Both it and the no-clause case have the same load-bearing fact
  -- the `m0` floor really is the line in force, unlike the expired shape
  where `m0` is only a crash guard. So they share a branch, and only the
  clause tail differs: `window starts in 15h00m` against `window start
  unknown`. Note the arithmetic, which is easy to get wrong: a clause 20h out
  on a 5h window starts **15h** out (`resets - window`), not 20h. The
  variable-width `human_delta` sits at the END of the field so the hand-tuned
  columns to its left are untouched.

  Accepted, not overlooked: `hook.log` still reads `session 50% over floor 5%`
  for this case, which is true but does not mention that a clause exists 15h
  out. The `status` row carries that diagnosis instead, and the log line is
  the one the hot path writes on every braked call -- not the place to spend
  a second lookup.

- **E13. A non-positive `window_seconds` takes the floor shape, not the void
  shape.** `window_seconds` is read back from `state.json` unvalidated; `0`
  raised `ZeroDivisionError` into `hook.main`'s fail-open (section 1.4's
  hazard reached directly) and `-5` produced `elapsed -9.0, allowed -778%`,
  which `now < start` cannot catch because a negative window puts `start`
  after `resets`. Folded into the floor guard, before the division.

  Why the floor and not void: garbage in `window_seconds` does not make the
  *pct* garbage. The pct is an independent and typically fresh measurement;
  only the window POSITION is destroyed, and the floor is already this
  codebase's answer to "fresh pct, no usable position". It still brakes
  whenever `pess > m0`, so nothing is allowed that a missing clause would not
  also allow. Void was rejected on two grounds: it asserts a specific
  falsehood (the window has not rolled -- its length is unreadable), and
  `expired: True` triggers E6's forced refresh on the FIRST PASS OF EVERY
  INVOCATION, so a daemon bug writing a persistently bad `window_seconds`
  would cost a ~2s subprocess plus a blind hold per tool call forever, even
  at 3% usage. Load-time validation was rejected for splitting the judgement
  into a second place, which is what centralising `bucket_pace` was for.

- **E14. The staleness-handoff test is rescued, not deleted, and pins the
  mechanism rather than the values.** `held` takes a per-pass age
  (`age=lambda t: 5 if t == EXPIRED_NOW else 9999`) so the snapshot can be
  fresh on pass 0 and stale after -- impossible while `snapshot_age` was
  patched to a constant, which is what made the original vacuous. It covers
  E6's division of labour, which nothing else does: the force looks once on a
  fresh snapshot, then the staleness gate and backoff take over. Its
  neighbours cover the `first` gate under success and under failure, but both
  use a constant age and so neither sees the handoff.

  On the polishing tax: the assertion is `gaps == list(hook.REFRESH_BACKOFF[1:5])`,
  which pins the *schedule mechanism* -- failed refreshes escalate rather
  than retry every chunk, the politeness guarantee to a down `/usage` -- and
  NOT the literal values, which are tuning. Do not add a literal-values test.

---

## 3. Phases

### Phase 1 -- behaviour

Files: `src/niceclaude/_shared.py`, `src/niceclaude/hook.py`,
`src/niceclaude/cli.py`, `src/niceclaude/plot.py`, `tests/test_niceclaude.py`.

1. `RESET_JITTER` to `_shared.py`; `cli.py` and `plot.py` import it (E1).
2. `bucket_pace`: the expiry test, the jitter-zone clamp, the total expired
   shape (E1, E2, E3).
3. `decide`: the explicit expired arm and reason string (E4); `expired` on the
   verdict when an enforced bucket is expired (E6).
4. `run`: the forced first-pass refresh (E6).
5. `cmd_status`: an expired branch in the row printer, before the
   `elapsed is None` branch, reading e.g.
   `window reset 51h ago -- reading is void` (E3).
6. `plot._pace`: check `expired` explicitly alongside `elapsed is None` (E5).
7. Tests (section 4).

### Phase 2 -- docs

`README.md`, `CHANGELOG.md`, `harness/design-decisions.md` (new section 23),
`harness/test-matrix.md` (cases from 17), `harness/README.md` (index this
file), and this file's status block.

---

## 4. Tests

**Citation convention, learned the hard way.** A test docstring cites the
**decision** it pins -- an E-number, or a section of this file. It never cites
a review-round finding number. Round 2's additions cited "T10".."T14", which
were the finding numbers in that round's message; every one of them already
named a *different* test in the list below, and T14 named nothing at all.
Finding numbers are per-round and meaningless the moment the round ends, which
is the same reason this file cites symbols rather than line numbers.

Beyond "a window that rolled under a reading":

1. The 1.3 repro: an expired bucket at 95% no longer produces a `soft` hold
   with a past `wake_at`.
2. E9: expired session + live-over week -> hold is confident, `blind` False.
3. E10: a latched `hard` descent whose window expires stays latched and goes
   blind, not free.
4. `_unit_pace(1.0)` returns the final line height and does not raise (E5).
5. `_pace` on a sample whose `resets_epoch` precedes its own `ts_epoch` by less
   than `RESET_JITTER` -- draws; by more -- returns None (E1, E5).
6. The jitter boundary both sides: `resets + RESET_JITTER` exactly is not
   expired; one second past it is.
7. `elapsed` never exceeds 1.0 for any input (E2).
8. The `status` row text for an expired bucket (E3).
9. E6: an expired enforced bucket forces a refresh on the first pass even when
   the snapshot is younger than `NEAR_STALE`, and does **not** refresh again on
   later passes.
10. The expired shape is total: it carries exactly the same key set as a live
    shape (E3, guards 1.4). Pinned by the *literal* key list in
    `test_band.py`, with the expired shape in the loop -- an equality among
    shapes from the same function would pass if all of them lost the key.

Added after round 1 review:

11. The jitter zone is where 1.3's soft-hold defect actually lives, so test 1
    must be parametrized across it: `resets + 30`, `resets + RESET_JITTER`,
    and `resets + 600`, asserting `wake_at > now` at all three. The first two
    exercise the clamped-but-live path, which expiry never reaches.
12. `elapsed` is within `[0.0, 1.0]` for any input, including a reset clause
    6h and 10h further out than one window (E2's low-end clamp).
13. Expiry on a `week` window, and under an `--enforce` subset both ways: an
    ignored expired bucket must leave the verdict's `expired` unset, an
    enforced one must set it. The round-1 version of this test was vacuous --
    its "expiring" bucket's reset was 2.5h in the future and the non-braked
    verdict carries no `expired` key at all, so it passed with the feature
    removed.

---

## 5. Out of scope

- `burn` segments by raw `resets_epoch` identity and never calls
  `bucket_pace`; `cmd_check`'s roll detection works on reset-clause deltas.
  Both are unaffected -- confirmed by reading, no change.
- The `fx` inversion in `plot.render` can exceed 100 for jitter-zone samples
  today. E2's clamp fixes that incidentally; no separate work.
- `plot.render`'s idle-span shading never renders. `run.append(...)` precedes
  the transition check, so `ax.axvspan(xs[0] if not run else x, x)` always
  evaluates to a zero-width `axvspan(x, x)` and the `xs[0]` arm is
  unreachable. Pre-existing, but this change routes more samples into it: a
  sample whose reset clause lags its own `ts_epoch` by more than
  `RESET_JITTER` now returns None from `_pace` and becomes idle where it
  previously drew an extrapolated line. Filed as sighting 18 and **not fixed
  here** -- it is pre-existing rot and belongs in its own change. `_pace`'s
  docstring must not cite shading that does not happen.
