# Open questions

The live edge of the work. Read this first when resuming.

Status as of the end of the first development weekend: the **mechanism** is
thoroughly proven, the **policy** is not. See §1 and §8 — those are the two that
matter.

---

## 1. ~~Windows is unverified — the deployment blocker~~ — RESOLVED

**Resolved:** `windows-results.md` records the first Windows run. The hook is
invoked synchronously and blocks, so the freeze works. The three Windows-only
bugs it found are fixed, with regression tests. Windows has since been the
main development and daily-use platform. The original entry follows.

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

**Update 2026-10-05: re-run over the long history.** `niceclaude burn`,
35,599 samples, 15-minute bins, median sampling interval 60 s (so the
daemon's continuous baseline, not activity-driven):

| Bucket | Average (idle incl.) | Busy p90 | Line rises | Duty cycle |
|---|---|---|---|---|
| `session` | 2.58 %/h | 32.0 %/h | 20.0 %/h | 62% |
| `week:Fable` | 0.59 %/h | 12.0 %/h | 0.60 %/h | **5%** |
| `week:all models` | 0.49 %/h | 8.0 %/h | 0.60 %/h | **7%** |

- **The weekly lines are still the governors over time.** Their duty cycles
  are 5–7%, against 62% for the session line. `week:Fable` averages
  0.59 %/h against a line rising at 0.60 %/h, so across 1,592 hours,
  consumption ran almost exactly on the pace line. That is what a pacer
  holding it should produce.
- **The session line does most of the *braking*.** In the live `hook.log`
  (§8) it was the first hot bucket in 335 of 470 brakes, and `week:Fable`
  in 127. Busy work runs at a p90 of 32 %/h against a line rising at
  20 %/h, so bursts cross it often. Those holds are cheap: the line
  catches up fast.
- Both readings stand. "Tuning session margins is close to pointless" was
  too strong: they decide how often work is interrupted. The weekly margins
  decide how much work happens. One caveat on the brake count:
  `solidstate` runs `m1=0.5` and enforces only `session` and `week`, which
  tilts it towards `session`.
- The run reported `corrupt JSON at log line 35049`; see §14.

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

**Update 2026-10-05:** §8 now has real brakes, and the session line produced
most of them (335 of 470), so the session margins do affect behaviour. The
folders in use tune their own margins (`solidstate`: `m0=20`, `m1=0.5`), and
none has run the defaults long enough to judge 5 and 8 themselves. The
defaults remain unvalidated.

---

## 6. Not yet built

Done since first draft: plotting (`niceclaude plot`), burn-rate analysis
(`niceclaude burn`), daemon supervision (`deploy/`), the fan-out gate
(`--fanout-reserve`), and the pidfile/`stop` lifecycle.

Since done as well (checked 2026-10-05): `plot.py` has tests
(`tests/test_plot.py`), CI runs the suite on push
(`.github/workflows/tests.yml`), and the repo has a git remote and a
`LICENSE`. A PyPI publishing workflow exists (`publish.yml`, `RELEASING.md`).
Whether a release has been made is not recorded here.

Still missing:

- `UserPromptSubmit` and `Stop` hooks remain unused. Genuinely optional.

---

## 7. Behaviour after a long freeze is unexamined

An agent resuming after hours holds a plan formed before the gap — files may have
changed, branches moved, the world turned. Nothing has been thought about here at
all. Possibly out of scope; possibly the most interesting remaining problem for
genuinely unattended multi-day runs.

**Deferred by the user, 2026-10-09:** "might have value, but I am not
interested in it currently." Do not propose it unprompted. The sketch, for
when it is picked up:
- §8's data says it is real. Holds released by `line-caught-up` have a
  median of 37 min, a p90 of 2.2 h and a maximum of 44 h.
- The cheapest form leaves holds as they are. When a hold longer than some
  threshold (say 15 min) releases, the hook's `PreToolUse` output tells the
  model how long it was paused, and that files, branches and processes may
  have changed.
- Phase 0 would check that this output actually reaches the model.
- The open cost question is whether the re-checking it prompts spends more
  tokens than it saves.

---

## 8. ~~The pace line has never braked anything in anger~~ — RESOLVED by real use

**Resolved 2026-10-05, from the live `hook.log`** (2026-08-17 to 2026-10-05,
Windows, default account). It is now real use, not forced policies. Paced
folders: this repo, `solidstate` and `solidstate2`, each with its own margins
and band.

| Hold ended by | Count | Median | p90 | Max | Total |
|---|---|---|---|---|---|
| `band-release` (one band_delay) | 2,102 | 120 s | 240 s | 253 s | 89 h |
| `line-caught-up` | 248 | 37 min | 2.2 h | 44 h | 391 h |
| `max_delay-release` | 211 | 240 s | 240 s | 246 s | 12 h |
| `unpaced` (kill switch or policy edit) | 10 | 11 min | 48 h | 48 h | 97 h |

- 2,604 holds started: 470 `brake` lines (8 of them escalations from a
  throttle) and 2,170 `throttle`. Only 4 lines mention `BLIND`.
- 2,572 releases leave about 32 holds (1.2%) with no `release`. The log
  cannot say whether the harness killed them at the hook timeout, the
  session was closed while frozen, or they are still running. The user has
  accepted this rather than chase it (2026-10-09): the hook's timeout is 48
  hours, and an agent let go after a 48-hour hold does little harm.
- First token of each `brake` reason, i.e. the first hot bucket: `session`
  335, `week:Fable` 127, `week:all models` 3, and 5 others (blind or no
  buckets). In this use the 5-hour line did most of the braking. See §3.
- Nearly all of this is from September onwards: 5 holds in August, 2,283
  in September, 352 so far in October.

So the line governs real work, and holds end by every route the design
provides. What this does not show is §8's other question: whether a paced
agent's work stays *useful* across its holds. Nothing here measures that,
and §7 is the part of it that is unexamined.

The original entry follows.

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

## 9. ~~Detect the model per call, not per folder~~ — RESOLVED

Built 2026-10-05 in 1711dfd as `--model detect`, to the plan in
`model-detection-plan.md`; the decisions are `design-decisions.md` §20. The
hook reads each caller's model from its own transcript (`detect_model`,
`model_family` in `hook.py`), so an Opus organizer and its Fable subagents
answer to different lines. The open points below were settled there: no
precedence rule, since `detect` is a value of `--model` (D1); `SubagentStart`
is not detected, and the subagent's first tool call is (D2); ids map to
families through `model_family`; the read is bounded at 8 MiB (D6); and
`hook.log` carries a `model=` tag (D7). Tests are in
`tests/test_model_detection.py`. The original entry, and the proof of concept,
follow.

Filed 2026-10-02 at the user's request, to be built later; it is not part of
issue #1.

**Planned 2026-10-05:** `model-detection-plan.md`. Phase 0 is done and
recorded in `platform-findings.md` §16.

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

The user's working proof of concept, verbatim (2026-10-02). This is the
approach to port, not a question of whether it can be done. Write it in pure
Python for portability, without `jq` or `tac`.

```bash
#!/usr/bin/env bash
# resolve-model.sh — prints the model for whoever triggered this hook
input=$(cat)
main=$(jq -r '.transcript_path' <<<"$input")
sid=$(jq -r '.session_id' <<<"$input")
aid=$(jq -r '.agent_id // empty' <<<"$input")

last_model() { tac "$1" 2>/dev/null | jq -r 'select(.type=="assistant") | .message.model // empty' | head -n1; }

if [[ -n "$aid" ]]; then
  t="$(dirname "$main")/$sid/subagents/agent-$aid.jsonl"
  [[ -f "$t" ]] || t=$(find "$(dirname "$main")" -name "agent-$aid.jsonl" 2>/dev/null | head -n1)
  model=$(last_model "$t")
else
  model=$(last_model "$main")
fi
echo "${model:-unknown}"
```

It reads the transcript backwards (`tac`) and stops at the first assistant
record. In Python, seek to the end and read blocks backwards until a
parseable assistant line turns up, so the hot path never reads a whole
transcript.

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

---

## 12. ~~Sighting: a declared model with no bucket, enforcing only `model`, brakes blind forever~~ — FIXED

**Fixed 2026-10-05**, at the user's request, the same way as D9 in
`model-detection-plan.md`. With `model` the only window enforced, `decide`
now lets a call that matches no per-model row run free, under any declared
model, and not only under `detect`. Tests:
`test_declared_enforcing_only_model_*` in `tests/test_model_detection.py`.
The original entry follows.

Filed 2026-10-05, found in the Phase 1 review of model detection.
It predates `detect` and does not depend on it.

A rule such as `{"model": "opus", "enforce": ["model"]}` names a model that
has no per-model bucket (no `week:Opus` exists). `decide`'s enforced set is
then empty, so every call takes the hard "no usable buckets in snapshot"
blind brake. Under the default `max_delay` (none) that hold is uncapped and
runs to the harness timeout. The `detect` form of the same case is fixed in
`decide`. Under `detect` with only `model` enforced, a caller with no
matching row runs free (`model-detection-plan.md` D9). At the time of
filing, the declared form was not fixed.

---

## 13. ~~Sighting: stale test counts and "not yet built" items in the docs~~ — FIXED

**Fixed 2026-10-05.** As suggested below, the test counts were removed from
`README.md` *Tests* and `test-matrix.md`, not updated, so they cannot drift
again. §6 now records what has been built since, and §1 is marked resolved.
The original entry follows.

Filed 2026-10-05, seen while writing model detection's Phase 2 paperwork.
Not fixed, since it is unrelated to that work. The suite now has 735 tests,
but `README.md` *Tests* says 388, `test-matrix.md` *Automated first* says 52,
and §6 above says "Running the 52 tests". §6 also lists "No CI" and "No git
remote, no LICENSE", while the repo now has a git remote (`origin`),
`LICENSE`, and `.github/workflows/tests.yml`. A count that drifts this
easily may be better left out than kept in step.

---

## 14. Sighting: a stray fragment in `usage.jsonl` — CONTAINED

**Contained 2026-10-05, by the user's choice: hold the invariant, do not
lock.** Locking appends across Windows, POSIX and containers is far harder
than the problem deserves. Instead, a bad line costs that line and nothing
else. The hook never reads `usage.jsonl`, since it paces from the atomically
replaced `state.json`, so pacing was never at risk. `load_log`, the one
reader (`check`, `burn`, `plot`), had two gaps, both now closed:

- A fragment splitting a multi-byte character, such as `/usage`'s middle
  dot, raised `UnicodeDecodeError` out of the read and lost the whole log.
  The log is now decoded with `errors="replace"`.
- A fragment that is itself valid JSON (`true`, a number) was returned as a
  record, and `burn` would raise on `rec["exit_code"]`. Non-objects are now
  skipped.

Pinned by `tests/test_log_corruption.py`. Seven of its eight tests fail
against the old reader. The fragments themselves will keep appearing; that
is accepted. The original entry follows.

Filed 2026-10-05, seen when `niceclaude burn` reported `corrupt JSON at log
line 35049` in the live default account's `usage.jsonl`.

- Line 35049 is 8 bytes: `: true}`.
- Line 35048, before it, is a complete record (2026-10-02T20:12:37Z, 2,549
  bytes) that parses and ends `"parse_ok": true}`. So the fragment repeats
  the last bytes of a record that is itself intact.
- Line 35050 is a normal record, three minutes later.

It is harmless as it stands, because `load_log` skips the line and says so.
The likely cause is two appends to `usage.jsonl` colliding, from the daemon
and an on-demand refresh by the hook. If so, a worse collision could tear a
real record, not just repeat a tail. Worth checking how `append_log` writes
(one `write` of the whole line, or several; with what open mode) and
whether Windows append is atomic for that size.
