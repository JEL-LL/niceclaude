# `blind_delay` — a bounded wait for a hold that cannot see

A hold the hook cannot justify from data ends after `blind_delay` seconds and
lets the call through, logged as `blind-release`. Default 240 (4 minutes).
Symbols are cited by name, not line number.

**Status: done. Phase 1 (behaviour) — six same-model review rounds and a
Fable gate; 853 passed, 4 skipped. Rounds 1–4 each found a defect in the
blind-versus-justified classification, which led to THE BLIND RULE in
`hook.decide` and its matrix test. Phase 2 (docs) done: `README.md`,
`CHANGELOG.md`, `design-decisions.md` §11, §12, §17 and the new §22,
`test-matrix.md` cases 13–16, and this file in `harness/README.md`. Both ran
under `pipelines/ratchet.md`.**

**Test baseline before any change:** 754 passed, 4 skipped at a4c3312
(`uv run --with pytest pytest tests/ -q`, Linux, ~31s).

---

## 1. The problem

Today a blind hold has no ceiling but the harness's. With `max_delay` off (the
default) the hook holds until a usable reading appears or Claude Code kills it
at `HOOK_TIMEOUT` (48h). That is a stall-forever, and the user's rule
overrides the design that produced it:

> There should never be a stall forever situation. I would rather fail free
> than stall for forever.

It is not hypothetical. 870 of 11422 samples in the author's `usage.jsonl`
carry no buckets at all, and `hook.log` holds 41 `BLIND` lines.

What does **not** stall, and must keep not stalling: a 5-hour window that has
not started. `/usage` prints `Current session: 0% used` with no reset clause,
`bucket_pace` judges it against `m0`, and 0% is under the floor. 5571 samples
of exactly that shape, every one at 0%.

## 2. Why 4 minutes

The cost being bounded is not the wait, it is the cache. A hold that outlives
the prompt cache TTL (5 minutes) makes the next turn re-read its whole context
from cold, and an agent that is blind on every call would then pay that on every
call — burning budget while getting no work done. 240s keeps each blind hold
under the TTL, so a long outage degrades to a slow, cache-warm crawl rather than
a freeze or a cold-cache loop. Longer is opt-in via `on --blind-delay`.

## 3. Decisions

- **B1. Name and shape.** Policy key `blind_delay`, seconds, resolved with
  `off_or_num` exactly like `band_delay`: rule, then `defaults`, then
  `DEFAULT_BLIND_DELAY = 240` in `_shared.py`, added to `DEFAULT_POLICY`'s
  `defaults`. An explicit `null` is off: the old hold-until-seen behaviour. A
  negative value clamps to 0; 0 means "never hold blind" (release on the first
  blind pass). Existing `policy.json` files with no key get 240 — intended, this
  is the behaviour change.
- **B2. What counts as blind.** Any `hard` hold with no release that data can
  justify:
  1. `no usable buckets in snapshot` (already `blind: True`);
  2. `BLIND: snapshot Ns old and refresh failing` (already `blind: True`);
  3. **new:** a `hot` hold in which *every* hot entry is wake-less — a bucket
     `bucket_pace` returns None for (`unusable`), or one over the `m0` floor with
     no reset clause (`p["wake"] is None`). `decide` must report it
     `blind: True`. If any hot entry has a real wake time, the hold is a real
     brake and is not blind.

  A stale snapshot that is already over the line with a real wake stays a
  confident, non-blind brake (design-decisions §12); `blind_delay` does not
  touch it. Neither does a soft (band) hold.
- **B3. The clock.** Blind time is continuous blindness *within this
  invocation*. It starts at the start of `run` if the first decision is blind,
  so refresh time spent before the hold counts against it; if a hold turns
  blind mid-way it starts then; it resets if the hold stops being blind. The
  point is that one tool call never costs more than `blind_delay` of blindness.
  (A single in-flight `refresh_snapshot` can still overrun it by up to its own
  180s timeout — synchronous, out of scope, note it in the docs.)
- **B4. Caps combine.** `blind_delay`, `max_delay` and `band_delay` are all
  ceilings; the tightest in force wins. When `max_delay` fires first while
  blind, the existing `max_delay-release-WHILE-BLIND` label stays. When
  `blind_delay` fires, the release reason is `blind-release`. The nap is
  bounded by the blind deadline the same way it is by `cap`, never below 1s.
- **B5. Next call.** No memory across invocations: the next tool call refreshes
  and decides afresh, and if still blind holds up to `blind_delay` again.
- **B6. CLI.** `niceclaude on --blind-delay SECONDS` / `--no-blind-delay`,
  mutually exclusive, written by `cmd_on` the way `band_delay` is (explicit
  null for off). `status` prints a `blind_delay` line beside `band_delay`, and
  its blind verdict and its `unsolvable` hold text say when `blind_delay` lets
  the call through (or that it is off). `on --help` epilog mentions it.

## 4. Phases

### Phase 1 — behaviour (code + tests)

Files: `src/niceclaude/_shared.py`, `src/niceclaude/hook.py`,
`src/niceclaude/cli.py`, new `tests/test_blind_delay.py`, and any existing test
whose expectation B2.3 legitimately changes (say which and why). Mark this
plan's status line.

Tests must cover at least: default 240 applies with no key anywhere; rule and
defaults override; explicit null holds past 240 (the old behaviour);
`blind-release` fires for each of the three B2 kinds; a non-blind hard brake
with stale-but-over data is NOT released by it; a soft hold is not affected;
mixed hot (one wake-less, one real) is not blind; B3 clock reset when a hold
turns un-blind and back; tightest cap wins against `max_delay`;
`on --blind-delay/--no-blind-delay` round-trip and mutual exclusion; `status`
shows the line. Use the existing fake-clock / monkeypatch style in
`tests/test_max_delay.py` and `tests/test_band.py`; no real sleeps.

### Phase 2 — docs

`README.md` (settings table / knobs), `CHANGELOG.md` Unreleased (behaviour
change first: blind holds now end after 4 minutes by default),
`harness/design-decisions.md` — amend §11 and §12 and §17 where they say a
blind hold is uncapped, and add §22 recording this decision and the user's
quote; `harness/test-matrix.md` new case(s); `harness/README.md` file table
gets this plan. Mark this plan done.
