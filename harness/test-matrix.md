# Test matrix

Behaviours that must hold. All of these run in a few minutes and **cost no
tokens** except the end-to-end case, which costs one trivial `-p` call.

Run this first on any new platform.

## Automated first

Most of the logic below is now covered by a real suite: no network, no tokens,
and a few seconds to run:

```bash
uv run --with pytest pytest tests/ -q
```

It covers the parser variants, `model_matches`, path resolution and prefix
traps, and the whole of `hook.decide` (pace line, blind/degraded handling,
freshly-rolled windows, and both jq-falsiness regressions). Run it before
anything else; the manual cases below then exercise the parts a unit test
cannot — real process freezing, the daemon, and the installed entry points.

---

## Setup

```bash
niceclaude install
mkdir -p /tmp/tp/sub /tmp/tpXTRA
niceclaude on  /tmp/tp --model opus
niceclaude off /tmp/tp/sub
```

Craft a snapshot at `<data>/state.json` with a fresh `ts_epoch` (the hook treats
anything older than 180s as stale and will refresh it from the server, replacing
your fixture):

```json
{"ts_epoch": <now>, "ts": "now", "ok": true, "buckets": {
  "session":         {"pct": 5, "resets_epoch": <now+14400>,  "window_seconds": 18000,  "label": null},
  "week:all models": {"pct": 5, "resets_epoch": <now+120000>, "window_seconds": 604800, "label": "all models"},
  "week:Fable":      {"pct": 99,"resets_epoch": <now+120000>, "window_seconds": 604800, "label": "Fable"}}}
```

Drive the hook directly:

```bash
printf '{"cwd":"/tmp/tp","hook_event_name":"PreToolUse"}' | timeout 3 niceclaude-hook
# exit 0   = allowed
# exit 124 = still sleeping (braking) when the timeout fired
```

---

## Cases

| # | Setup | cwd | Expected |
|---|---|---|---|
| 1 | session 5%, week 5% | `/tmp/tp` | **allow** |
| 2 | session 5%, week 5% | `/tmp/tp/sub` | **allow** — explicitly unpaced subtree |
| 3 | session 5%, week 5% | `/tmp/tpXTRA` | **allow** — must NOT match the `/tmp/tp` rule |
| 4 | session 5%, week 5% | `/tmp` | **allow** — no rule |
| 5 | session **50%**, week 5% | `/tmp/tp` | **brake** |
| 6 | session **50%**, week 5% | `/tmp/tp/sub` | **allow** — unpaced subtree free while parent brakes |
| 7 | Fable 99%, `--model opus` | `/tmp/tp` | **allow** — unmatched model's bucket ignored |
| 8 | Fable 99%, `--model fable` | `/tmp/tp` | **brake** — matched model's bucket enforced |
| 9 | over the line, `niceclaude global off` | `/tmp/tp` | **allow** — kill switch |
| 10 | session `pct 0`, `resets_epoch null` | `/tmp/tp` | **allow** — freshly rolled window must not brake |
| 11 | Fable 99%, `--model detect`, payload `transcript_path` → a file whose last line is `{"type":"assistant","message":{"model":"claude-fable-5-1"}}` | `/tmp/tp` | **brake** — detected Fable caller; `hook.log` ends `model=fable(detected)` |
| 12 | as 11, but the model is `claude-opus-5-5` | `/tmp/tp` | **allow** — detected Opus caller has no per-model bucket |
| 13 | `"buckets": {}`, `on /tmp/tp --blind-delay 5`, run under `timeout 15` | `/tmp/tp` | **allow after ~5s** — blind hold fails free; `hook.log` ends `release ... (blind-release)` |
| 14 | session **50%**, `resets_epoch null`; `--blind-delay 5`, `timeout 15` | `/tmp/tp` | **allow after ~5s** — over the floor with no reset clause is wake-less, so blind |
| 15 | session **50%**, week 5%; `--blind-delay 5`, `timeout 15` | `/tmp/tp` | **brake** (exit 124) — a real wake makes it a confident brake, which `blind_delay` never touches |
| 16 | as 13, but `--no-blind-delay` | `/tmp/tp` | **brake** (exit 124) — the old hold-until-seen behaviour |
| 17 | session **3%**, `resets_epoch` = now minus 52h; `--blind-delay 5`, `NICECLAUDE_BIN=/bin/false`, `timeout 15` | `/tmp/tp` | **allow after ~5s** — the window rolled under the reading, so it is void; `hook.log` reads `session: window reset since this reading` then `release ... (blind-release)` |
| 18 | session **50%**, `resets_epoch` = now + 20h (a 5h window that has not begun); `--blind-delay 5`, `timeout 15` | `/tmp/tp` | **allow after ~5s** — judged against the `m0` floor with no wake to solve, so blind; `hook.log` reads `session 50% over floor 5%` — a brake reason, where the old code counted down a confident 18h hold |
| 19 | session **50%**, `"window_seconds": "18000"` (quoted); `--blind-delay 5`, `timeout 15` | `/tmp/tp` | **allow after ~5s** — a non-numeric window length takes the same floor; `hook.log` reads `session 50% over floor 5%` then a `(blind-release)`, and never `ERROR ... -- failing open`, which would mean the hook raised and the folder ran unpaced |
| 20 | session **50%**, `"window_seconds": 600`, `resets_epoch` = now + 305 (line 50.0%, throttle 43.0%, so just over); `week:all models` **50%**, `resets_epoch` = now + 278069 (line 52.0%, throttle 45.0% — in its band, never crossed); `on /tmp/tp --band-delay 5`, `timeout 120` | `/tmp/tp` | **allow after ~70s** — the hold is the session's own descent to the session throttle line, and ends there; `hook.log` ends `release ... (band-release-after-descent)`. The week's own throttle crossing is 11.6h out, and that is what the old code ran down to |
| 21 | as 20, but `--band-delay 90`, `timeout 180` | `/tmp/tp` | **allow after ~90s**, not ~160s — the band the descent is left sitting in costs `max(0, band_delay - (now - brake_start))`, so the whole hold is one `band_delay`, not a descent plus a fresh one |

Cases 20 and 21 are the only ones here whose fixture has a deadline: the
session is over its line for 22s after you write it, and then its own brake
line rises past the reading. Drive the hook inside that window. Miss it and
the session is already in its band, nothing latches, and the run ends in a
plain `band-release` with exit 0 -- the same exit code as a pass, which is
why this is worth saying rather than leaving to the reader's clock.

Case 3 guards component-wise prefix matching. Case 10 guards the fail-safe
exception in `design-decisions.md` §11 — the naive version brakes hardest
exactly when headroom is greatest.

Cases 11–12 re-check `--model detect` (`design-decisions.md` §20). The
automated cover is `tests/test_model_detection.py`: the main and subagent
paths and the search fallback, the `SubagentStart` skip, torn, synthetic,
split, CRLF and non-UTF-8 records, the 8 MiB cap, every id seen in the wild,
and an Opus parent with a Fable subagent end to end through `run`. What no unit test
can check is what Claude Code actually writes, and when. For that, re-run
`harness/probes/model_probe.py` (the Phase 0 probe, `platform-findings.md`
§16) after a Claude Code upgrade:
a hook registered for `PreToolUse` and `SubagentStart` through `claude
--settings probe.json -p ...`, from an unpaced scratch dir, logging key names
and transcript facts only, never content. It must still show no `model` key
in any payload, no subagent transcript at `SubagentStart`, and the
subagent's own record at its first `PreToolUse` on the derived path.

Cases 13–16 re-check `blind_delay` (`design-decisions.md` §22). Write each
fixture just before driving the hook: a snapshot at or over the line is
refreshed once it is older than 15s (`NEAR_STALE`), which would replace it with
live data. Put the default back afterwards with
`niceclaude on /tmp/tp --blind-delay 240`. The
automated cover is `tests/test_blind_delay.py`: the default and its overrides,
`blind-release` for each kind of blind hold, a stale-but-over brake and a soft
hold left alone, a mixed hot hold that is not blind, the clock across a hold
that turns un-blind and back, the latch, the tightest cap winning, and the
`on` and `status` surfaces.

Cases 17–19 re-check a reading the hook can no longer place against a running
window (`design-decisions.md` §23). All three allow at the cap, so read `hook.log`
rather than the exit code: it is the brake reason that says which hold you
got. Case 17 needs `NICECLAUDE_BIN` pointed at something that fails, because a
rolled window forces a refresh however fresh the fixture is — that force is
the behaviour under test, and the override is only what stops `niceclaude
refresh` from replacing the fixture with live data. Cases 18–19 need no such
thing: a fixture written just before the run is inside `NEAR_STALE`, so
nothing re-reads it. Put the default back afterwards with
`niceclaude on /tmp/tp --blind-delay 240`. The automated cover is in
`tests/test_niceclaude.py`: the jitter boundary from both sides, `elapsed`
never leaving `[0, 1]`, the void shape's key set and its fail-toward-holding
fields, `decide`'s own reason for it, a live weekly sibling keeping the hold
confident, a latched descent going blind rather than free, the forced refresh
and its handoff to the staleness gate, every non-numeric `state.json` field
returning rather than raising, and the `status` rows for a void window and for
one that has not begun.

Cases 20–21 re-check the per-bucket latch (`design-decisions.md` §24), and
both are watched on a wall clock: what they test is which instant a hold is
aimed at, which no exit code reports. The session fixture's 600-second window
is the one artificial part, and it is there to make the descent watchable — it
puts the whole 5→92% climb of the brake line into ten minutes, so the
seven-point descent takes under a minute instead of twenty-four. Nothing
refreshes underneath them: a fixture written just before the run stays inside
`MAX_STALE` for the whole hold, so what you are reading is the hook deciding on
the numbers you wrote. Put the default back afterwards with `niceclaude on
/tmp/tp --band-delay 180`. The automated cover is in `tests/test_niceclaude.py`
and `tests/test_blind_delay.py`: the motivating scenario itself, a crossed
bucket running down to its own throttle, the wake target across the whole
over-to-band crossing rather than at one instant, the soft cap's own release
label, and which entries may arm a latch at all.

Latency should be **~20ms** for every allow case except 13, 14 and 17–21.

---

## Parser regression

```bash
niceclaude check
```

Re-parses **every stored sample** with the current parser and asserts:

- usage never decreases within a window (a decrease with no window roll means a
  misparse);
- reset timestamps stay stable, within 120s to absorb the server's rendering
  jitter;
- no line starting with `Current` failed to parse;
- no unrecognised line carries limit-ish vocabulary.

Expected: `N samples over Xh; buckets: [...]` then `no anomalies`.

This is the whole reason raw stdout is stored on every record — a parser fix is
re-validated against all history, not just against new samples.

---

## Daemon lifecycle

```bash
niceclaude watch &      # starts, writes daemon.pid
niceclaude watch        # must REFUSE: "daemon already running (pid N)"
niceclaude stop         # must stop it
```

Do **not** manage it by process name; see `design-decisions.md` §13.

---

## End-to-end (costs one trivial call)

Force a brake against *live* data by making the line unreachable, so it survives
daemon refreshes — this is also the technique for probing boundary behaviour:

```bash
niceclaude on /tmp/tp --model opus --m0 0 --m1 99   # line ≈ 1%
cd /tmp/tp
claude -p "Run one bash command: echo DONE. Then stop." \
  --settings <config>/settings.json --allowedTools "Bash" &
sleep 25
# the claude process must still be alive — frozen
niceclaude off /tmp/tp        # or: niceclaude global off
# it should now complete with rc=0 and produce DONE
```

Expected `hook.log`:

```
21:19:02  brake    cwd=/tmp/tp session 11.0% over line 0.2%; week:all models 16.0% over line 0.8%
21:19:32  release  cwd=/tmp/tp after 30s (unpaced)
```

**Both lines must appear.** A brake with no matching release reads as a hang,
and `hook.log` is the only record of what the pacer did overnight.

Release latency is up to one `chunk` (default 15s) — that is also how long a
policy change takes to reach an already-frozen agent.

---

## Cleanup

```bash
niceclaude off /tmp/tp
niceclaude global on
rm -rf /tmp/tp /tmp/tpXTRA
```
