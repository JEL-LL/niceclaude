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

---

## 9. Detect the model per call, not per folder (idea, not scheduled)

Filed 2026-10-02 at the user's request, to be built later; it is not part of
issue #1.

**The problem.** A folder declares its model (`--model`), and the `model`
window is checked against that. A session that runs on one model but spawns
subagents on another, for example Opus with Fable subagents, gets paced on
the declared model's per-model weekly bucket for every call. Once the Fable
bucket is used up, every call starts lagging, the Opus ones included, or the
other way round, depending on what was declared.

**The idea.** The hook payload already says who is calling. `transcript_path`
and `session_id` are always there, and `agent_id` is there when the caller is
a subagent. The model is the `message.model` of the last `type == "assistant"`
record in the caller's transcript:

- main agent: `transcript_path`
- subagent: `<dirname(transcript_path)>/<session_id>/subagents/agent-<agent_id>.jsonl`,
  and if that is missing, search under `dirname(transcript_path)` for
  `agent-<agent_id>.jsonl`

The user's bash sketch reads the file backwards (`tac`) and stops at the first
assistant record. Write it in pure Python for portability: seek to the end and
read blocks backwards until a parseable assistant line turns up, so the hot
path never reads a whole transcript.

**Open points for when it is built:**

- Precedence. A declared folder model is either an override or a fallback
  for when detection fails (no assistant record yet, as on the first call of
  a fresh subagent, or a missing file). The likely answer is: detected wins,
  declared is the fallback, and `unknown` falls back to `week` alone.
- On the first call, a subagent's transcript may hold no assistant record
  yet. Check what `SubagentStart` and the first `PreToolUse` actually see
  (phase-0 style, measure it before relying on it).
- Mapping `message.model` (e.g. `claude-fable-5-1`) onto the bucket's display
  name: `model_matches` already does a word match, so check it against real
  ids.
- Cost: one backwards read per hook call, against the ~20ms budget.
- `hook.log` should record the detected model, so a hold can be explained.

---

## 10. TODO: Phase 0 Procedure B for issue #1 (needs a machine with two logins)

Filed 2026-10-02. The work machine has one Claude account, so this cannot run
there. Do it from a checkout on a machine with two accounts (the user's home
machine), then commit the results.

**What to run:** Procedure B in `per-account-state-plan.md`, §8, *Phase 0*.
Use Procedure A's probe hook (`--settings <scratch>/probe.json`, logging
`CLAUDE_CONFIG_DIR`, `hook_event_name`, `agent_id` and `transcript_path`,
nothing else) against the second account's config dir:

1. `CLAUDE_CONFIG_DIR=<dir>`: does the hook see the value? Where is
   `.claude.json` written?
2. `CLAUDE_CONFIG_DIR=<dir>` plus `CLAUDE_CODE_SUBPROCESS_ENV_SCRUB=1`: does
   the hook still see it?
3. `claude --config-dir <dir>`, but only if `claude --help` lists the flag.

Never copy credential files, and never print them.

**Where the answer goes:** `platform-findings.md` §15, and a review-log entry
in the plan, like X1–X2.

**What depends on it:** the code assumes the hook inherits
`CLAUDE_CONFIG_DIR` unchanged (the `ASSUMPTION (Phase 0, unverified)` comments
in `_shared.py`). If check 2 or 3 shows the variable lost, build the plan's
D10 payload fallback, which derives the config dir from `transcript_path`.
Until then, a hook that has lost the variable is keyed as the default account.

Also worth checking on a Mac with two accounts: `config_key` assumes that a
config dir resolving to `~/.claude` (through a symlink, say) is the default
login. On macOS the keychain entry may be keyed on the directory's spelling, so
it could be the same dir but not the same login.

---

## 11. ~~Sighting: `write_atomic` leaks `state.json.tmp.<pid>` on Windows~~ — FIXED

Fixed 2026-10-05. The cause was confirmed by reproduction: with a reader
holding `state.json` open in another process, `os.replace` raises
`PermissionError [WinError 5]`. The fix is in `cli.write_atomic`: `_replace`
retries for up to `REPLACE_RETRY_SECONDS`, a failed write removes its temp
file, and `publish_state` calls `_sweep_stale_tmps`, which removes
`state.json.tmp.<digits>` files older than `STALE_TMP_SECONDS` (10 minutes).
It goes by age rather than pid liveness, because a shared data dir can hold
another container's or host's pids, and it runs for state.json only, never in
Claude's settings dir. In
addition, `_watch_loop` survives a failed publish. Tests are in
`tests/test_write_atomic.py`. The original sighting follows.

Filed 2026-10-03. Not fixed, and not part of issue #1.

The live data dir (`%LOCALAPPDATA%\niceclaude`) held 39 stray
`state.json.tmp.<pid>` files, dated 2026-09-30 to 2026-10-02. That is
roughly one an hour of refreshing.

**Likely cause, unverified:** `cli.write_atomic` writes `<path>.tmp.<pid>` and
then calls `os.replace`. On Windows, `os.replace` raises `PermissionError`
while another process holds the target open, for example a hook reading
`state.json` at that moment. Nothing removes the temp file on that path, so
it is left behind, and that publish is lost: the snapshot stays one sample
older. On POSIX a rename over an open file succeeds, so this is Windows-only.

**To check:** whether the exception reaches `refresh`/`watch` and what they
log, and how often it happens.

**Likely fix:** retry `os.replace` briefly on `PermissionError`, then remove
the temp file in a `finally` if it still exists. One leftover temp file per
pid would also be enough for a later run to clean up.
