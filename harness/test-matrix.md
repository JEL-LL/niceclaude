# Test matrix

Behaviours that must hold. All of these run in a few minutes and **cost no
tokens** except the end-to-end case, which costs one trivial `-p` call.

Run this first on any new platform.

## Automated first

Most of the logic below is now covered by a real suite — 52 tests, no network,
no tokens, runs in under a second:

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

Case 3 guards component-wise prefix matching. Case 10 guards the fail-safe
exception in `design-decisions.md` §11 — the naive version brakes hardest
exactly when headroom is greatest.

Latency should be **~20ms** for every allow case.

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
