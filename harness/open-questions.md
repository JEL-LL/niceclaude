# Open questions

The live edge of the work. Read this first when resuming.

Status as of the end of the first development weekend: the **mechanism** is
thoroughly proven, the **policy** is not. See §1 and §8 — those are the two that
matter.

---

## 1. Windows is unverified — the deployment blocker

The code is platform-neutral (`%LOCALAPPDATA%`/`%APPDATA%`, `normcase`+`normpath`
paths, `ctypes` liveness, `taskkill`, quoting for spaced hook paths) but **not one
line has executed on Windows.**

**→ `harness/windows-handoff.md` is a complete, self-contained brief for an agent
on a Windows machine.** It has the version banner to capture, nine numbered
checks with expected outcomes, and a list of known non-issues not to chase. The
critical one is check 4: whether Claude Code invokes the hook synchronously and
blocks on it, which is the entire freeze mechanism and is not documented
behaviour.

Results go in `harness/windows-results.md`.

---

## 2. The over-limit rendering — mostly answered from source, narrow residual

Reading the renderer out of the binary (`platform-findings.md` §11) settled the
substance of this:

- **There is no over-limit branch.** At the limit it emits `100% used` in exactly
  the same format.
- **It cannot hang on rate limits** — a pure formatter over an already-fetched
  status payload, with no inference call on the path.

What genuinely remains unknown is narrow:

- Do the `extra_usage` or `seven_day_opus` buckets appear when an account is over
  or on overage billing? Both exist in the schema; neither has been observed
  rendered.
- Does the overage preamble appear as expected? It is allow-listed already, but
  only against a hand-written fixture.

**Cheapest closer:** have someone whose account is already over run the capture
in `platform-findings.md` §13 and send back the file. Costs them nothing —
`/usage` consumes no tokens. Ask them to glance at it first: the advisory block
can name internal skills, plugins and MCP servers.

---

## 3. Burn rate — measured once, needs a longer baseline

`niceclaude burn` answers this. First reading, over 4h of heavy interactive Opus
work:

| Bucket | Average (idle incl.) | Busy p90 | Line rises | Duty cycle |
|---|---|---|---|---|
| `session` | 12.0 %/h | 24.0 %/h | 20.0 %/h | **83%** |
| `week:all models` | 1.41 %/h | 4.0 %/h | 0.60 %/h | **15%** |

**The asymmetry is the finding.** The 5-hour line rises at 20 %/h and nearly
keeps pace with heavy consumption, so it barely binds. The weekly line rises at
0.60 %/h and is the real governor. Tuning session margins is close to pointless;
weekly margins are what matter.

At this intensity a paced agent works ~9 minutes per hour, so a two-day absence
yields roughly 7 hours of real work. That answers "is this worth running" —
yes — but from a 4-hour sample of one workload. Re-run after a few days of
genuine background use.

---

## 4. ~~Multi-hour freezes~~ — RESOLVED

A real `claude -p` process was held **10811 seconds (3h)** against an unreachable
pace line, released cleanly, and its tool call then completed with `rc=0`. Full
log in `harness/freeze-validation.md`. Combined with the earlier 120s and 700s
runs at `timeout: 21600`, there is no evidence of any ceiling.

---

## 5. `m0` and `m1` defaults are guesses

5 and 8. Structurally sound (`m0` must exceed 0 or nothing starts; `m1` must
exceed the 1% quantization error) but the specific values have never been tested
against a real workload — see §8, which is why. Both are per-folder overridable,
as is `fanout_reserve` and `chunk`.

The burn-rate asymmetry in §3 suggests effort should go into the weekly margins;
the session ones barely affect behaviour.

---

## 6. Not yet built

Done since first draft: plotting (`niceclaude plot`), burn-rate analysis
(`niceclaude burn`), daemon supervision (`deploy/`), the fan-out gate
(`--fanout-reserve`), and the pidfile/`stop` lifecycle.

Still missing:

- **`plot.py` has no test coverage.** The other three modules are covered.
- **No CI.** Running the 52 tests on push is cheap and catches exactly the drift
  that bites shared tooling.
- **No git remote, no LICENSE, not on PyPI.** Distribution is unsolved;
  open-sourcing is pending an employer decision.
- `UserPromptSubmit` and `Stop` hooks remain unused. Genuinely optional.

---

## 7. Behaviour after a long freeze is unexamined

An agent resuming after hours holds a plan formed before the gap — files may have
changed, branches moved, the world turned. Nothing has been thought about here at
all. Possibly out of scope; possibly the most interesting remaining problem for
genuinely unattended multi-day runs.

---

## 8. The pace line has never braked anything in anger

**The most important caveat in this file.** Every brake ever observed was forced
with an artificial policy (`m0=0, m1=99`) to make the line unreachable.

The *mechanism* is proven from many angles: 3-hour freezes, subagent trees
freezing as a unit, the kill switch, blind/degraded handling, a 20ms hot path,
policy changes reaching a running agent.

The *policy* is untested. Across 3855 samples over 65 hours, **zero** samples
were above the line — utilization never came within 12 points of it, because the
machine was idle 87% of the time (`niceclaude plot`). So the pace line has never
actually had to govern anything.

Consequences:

- `m0`/`m1` are unvalidated in practice (§5).
- Whether a paced agent produces *useful* work or just stalls awkwardly is
  unknown. The 15% duty cycle in §3 is arithmetic, not observation.
- The graph's reassuring "0.0% above the line" is **weak evidence**, not strong.
  It says the line was never tested, not that the tool holds it.

**What would close this:** one genuinely busy background run — a real task, real
margins (`m0=5, m1=8`), enough work to push weekly utilization up to the line —
and then inspect `hook.log` for brake/release cycles and `niceclaude plot` for
the curve tracking the diagonal. Until that exists, treat the tool as
mechanically sound and behaviourally unproven.
