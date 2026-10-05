# Per-call model detection — plan for open-questions §9

Let a folder say `--model detect`, so the hook paces each call on the model of
whoever made it, rather than on one declared model. The approach is the user's proof of concept in
`open-questions.md` §9 (`resolve-model.sh`), ported to pure Python. It is not
re-evaluated here. Symbols are cited by name, not line number.

**Status: done. Phase 0 measured. Q1 settled (`--model detect`, D1). Phase 1
built in 1711dfd (`detect_model`, `model_family` and the wiring in `hook.py`,
D8's wording in `cli.py`, `tests/test_model_detection.py`), reviewed through
four same-model rounds and a Fable gate (D9 settled the last open behaviour).
Phase 2 done: README *Declaring the model*, `design-decisions.md` §20,
`open-questions.md` §9 marked resolved, `CHANGELOG.md`, and `test-matrix.md`
cases 11–12.**

**Scale check.** The only per-model bucket that exists is `week:Fable`, so in
practice this decides one thing per call: pace on the Fable line or not. It
stays expressed as a model designation, but nothing here may grow machinery
beyond what that binary needs.

**Test baseline before any change:** 643 passed at da308b7
(`uv run --with pytest pytest tests/ -q`, Windows, Python 3.13).

---

## 1. The problem

`decide` builds its enforced set from `entry["model"]`, the folder's
declared model, and matches it against bucket keys with `model_matches`. A
folder declared Opus that spawns Fable subagents never enforces `week:Fable`
on them. A folder declared Fable enforces `week:Fable` on its Opus main
agent too, so once that bucket is spent every call lags, Opus included.

## 2. The approach, as ported

`detect_model(payload)` in `hook.py` returns a model id, or `None`.

- **Main agent** (no `agent_id`): read `transcript_path`.
- **Subagent**: read `<dirname(transcript_path)>/<session_id>/subagents/agent-<agent_id>.jsonl`.
  If that is missing, search under `dirname(transcript_path)` for
  `agent-<agent_id>.jsonl` (the PoC's `find`).
- **The read** replaces `tac | jq | head -1`. It seeks to the end and reads
  fixed-size blocks backwards, carrying the partial first line into the next
  block. It returns `message.model` of the first line that parses as a record
  with `type == "assistant"`. Unparseable lines are skipped, and so is a torn
  final line from a write in progress.
- `model_family(id)` maps an id onto the word `model_matches` already
  compares: split on `-`, `.`, `_` and take the first alphabetic token after
  `claude`. So `claude-fable-5-1` gives `fable`, `claude-haiku-4-5-20251001`
  gives `haiku`, and `us.anthropic.claude-opus-…` gives `opus`.
  `model_matches` itself is unchanged.

## 3. Decisions

- **D1 — `detect` is a value of `--model`, not a precedence rule** (the
  user's answer to Q1). `--model fable` keeps today's behaviour exactly.
  `--model detect` reads the model per call. With no `--model`, no per-model
  bucket is enforced, as today. So there is no precedence to resolve. When
  detection finds nothing, the call simply has no per-model bucket. `session`
  and `week` still apply. (Superseded after Phase 2: `detect` is now the
  default `cmd_on` writes for a rule with no model; `design-decisions.md`
  §20 D1.) Stored as `"model": "detect"`, matched
  case-insensitively.
- **D2 — `SubagentStart` does not detect.** Its subagent transcript does not
  exist yet (Phase 0, all three runs). Under `detect` it gets no per-model
  bucket, and the hook skips the file check and the search. The search would
  run on every fan-out, always fail, and walk the whole project dir: 17 ms
  over 9,750 files warm, and one project dir here has 22,541. Here the port
  departs from the PoC, which would search and then print `unknown`. It
  also departs in D4 and D6, and in skipping a torn last line, which aborts
  the PoC's `jq` and leaves it printing `unknown`. The consequence is
  bounded: a Fable subagent launches past the Fable line, and its **first
  tool call** is held on it (Phase 0: that call sees its own record in every
  run).
- **D3 — a subagent never borrows its parent's model.** The parent's model is
  exactly the wrong answer in the Opus-with-Fable-subagents case. A subagent
  whose own transcript has no assistant record yet gets no per-model bucket
  for that call (D1).
- **D4 — `<synthetic>` is skipped**, and the read keeps going backwards past
  it. 13 such records were found in real transcripts. Accepting one would
  match no bucket and silently drop the model window.
- **D5 — lazy, once per invocation.** Detection runs only after
  `paced_entry` says the folder is paced, and only if `model` is in its
  enforce set. So unpaced folders pay nothing. It runs once per `run`, not
  per hold pass, because a frozen agent cannot change model while frozen.
- **D6 — bounded.** The backwards read gives up after 8 MiB and returns None.
  The largest run of non-assistant records measured is 2.5 MB, in 34,061
  transcripts. Any `OSError` or `ValueError` also returns None. Detection never
  fails the hook, and never fails it open either.
- **D7 — `hook.log` says which model and where it came from.** `brake` and
  `throttle` lines gain `model=<family>(detected|declared)`, or
  `model=none`, so a hold can be explained.
- **D8 — `status` and `on` change wording only.** `status` has no caller to
  detect from, so under `detect` it marks a model bucket `per call` rather
  than ENFORCED or ignored. The `on` note for an undeclared model now
  suggests `--model detect`. (That note was dropped when `detect` became
  the default.)

- **D9 — a missing per-model bucket is ignored under `detect`** (the user's
  call, settled during Phase 1 review). With only `model` enforced, a caller
  that matches no per-model row runs free, whoever it is. "If you are just
  supposed to hold on Fable and you don't have Fable information, you don't
  just stop and catch fire, you just run free." The case this lets through: a
  Fable call whose `week:Fable` row is unrendered, because its utilization
  is null, runs unpaced under `--enforce model`. The row was present in 35,330
  of 35,330 samples. Rejected alternatives:
  - a list of the families that have buckets (fails safe, but needs upkeep);
  - a blind hold, as for a declared model (it freezes the organizer).
  An enforced `session` or `week` bucket that is missing still means
  "cannot see", and brakes blind as before. After Phase 2 the user had the
  same rule applied to declared models, which fixes `open-questions.md` §12:
  the early return in `decide` no longer tests for `detect`.

## 4. Phase 0 results — measured, no code

Probe: `harness/probes/model_probe.py`, registered for `PreToolUse` and
`SubagentStart` through `claude --settings probe.json -p …`, run from the
unpaced scratch dir on Claude Code 2.1.287. Per event it logged payload key
*names*, the event, `tool_name`, `agent_id` and `agent_type`. For each
candidate transcript it logged: exists, size, assistant-record count,
distinct models, the last assistant `message.model`, and the backwards-read
time. It logged no tool input and no message content. Three runs: main on
Sonnet with a Haiku subagent, then twice on the default model (Opus) with an
inheriting subagent. Full write-up: `platform-findings.md` §16.

| Event | Subagent file | Assistant record | `message.model` |
|---|---|---|---|
| main, 1st `PreToolUse` | — | in runs 2–3; **absent in run 1** (66 KB file, 0 records) | runs 2–3 `claude-opus-5-5`; run 1 none |
| main, later `PreToolUse` | — | yes | `claude-sonnet-5-5`, `claude-opus-5-5` |
| `SubagentStart` | **does not exist**, in all 3 runs | — | — |
| subagent, 1st `PreToolUse` | exists at the primary path | yes, all 3 runs | `claude-haiku-4-5-20251001`, `claude-opus-5-5` |

- The first main-agent call races the transcript write, so "usually detected"
  is not "always". Under D1 such a call just has no per-model bucket.
- A subagent's primary path was right every time, and the search found the
  same file.
- There is still no `model` key in any payload.
- One backwards read took 0.2–0.35 ms.
- Ids seen across all local transcripts: `claude-opus-5-5`, `claude-fable-5-1`,
  `claude-opus-5`, `claude-opus-4-8`, `claude-fable-5`,
  `claude-haiku-4-5-20251001`, `claude-sonnet-5`, `claude-sonnet-5-5`, and
  `<synthetic>`. Only one per-model bucket has ever been recorded:
  `week:Fable`. So `model_family` must give `fable` for both Fable ids.
- **It works for subagents, which is the point of §9.** In run 1 the Sonnet
  parent's calls read `claude-sonnet-5-5`, while the Haiku subagent's calls
  read `claude-haiku-4-5-20251001` from its own transcript. An Opus organizer
  and a Fable subagent are therefore told apart on every tool call. The only
  blind spot is `SubagentStart` (D2).

## 5. Phases — confirmed

Two build phases. The original three split the detector from its wiring, but
with D1 the wiring is a few lines in `decide` and a value `on` accepts. A
detector committed without them would be dead code.

- **Phase 1 — detector and wiring.**
  - `detect_model` and `model_family` in `hook.py`.
  - `run` detects once, lazily (D5), only when the rule says `detect` and
    enforces `model`, and passes the family into `decide` as a parameter, so
    `decide` stays pure.
  - D7's `hook.log` field, and D8's wording in `cli.py`. `on --model detect`
    is accepted as is, because `--model` is free text.
  - Tests in a new `tests/test_model_detection.py`:
    - transcript fixtures for the main and subagent paths, and the search
      fallback;
    - the `SubagentStart` skip;
    - a torn last line, `<synthetic>`, a record split across a block
      boundary, CRLF, non-UTF-8 bytes, and the 8 MiB cap;
    - every id in §4;
    - an Opus parent with a Fable subagent, end to end through `decide`.
  - Keep green: everything. The one edit to an existing test is
    `tests/test_exempt.py` `fake_run`, which takes the new `payload`
    argument `main` now passes to `run`.
- **Phase 2 — paperwork.** `README.md` *Declaring the model*, a new
  decision in `design-decisions.md`, `open-questions.md` §9 marked resolved,
  `CHANGELOG.md`, and a `test-matrix.md` row.

Each phase goes through `pipelines/ratchet.md`: implement, fresh reviewers
until quiet, a Fable gate, then commit.

## 6. Questions for the user

- **Q1** — settled: `--model detect` (D1).
- **Q2** — settled by D1: `SubagentStart` under `detect` has no per-model
  bucket. Its first tool call does.
- **Q3** — settled: the two-phase split in §5. Phase 1 is built on it.
