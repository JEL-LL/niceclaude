# Platform findings

Empirical facts about Claude Code and `/usage`, with the evidence. None of this
is documented behaviour, so **re-verify after a Claude Code upgrade** —
`/usage` output already changed shape once during development.

Observed on Claude Code **2.1.232**, Linux.

---

## 1. `claude -p "/usage"` is the data source

There is no `usage` subcommand and no CLI flag, but the slash command runs
headlessly:

```
$ claude -p "/usage"
You are currently using your subscription to power your Claude Code usage

Current session: 11% used · resets Aug 14, 8:10pm (UTC)
Current week (all models): 14% used · resets Aug 16, 12am (UTC)
Current week (Fable): 2% used · resets Aug 15, 11:59pm (UTC)
```

- **~1.5s per call.**
- **Costs no tokens.** Percentages were identical across back-to-back calls; it
  is a server status query, not an inference call.
- **Account-global.** It reports usage across all machines and sessions, not
  just the local one. This is what makes foreground work automatically starve
  background work with no IPC between them.

---

## 2. Percentages are integers — confirmed at source

Distinct values observed across 100+ samples: `2, 5, 11, 14, 15, 16, ...` —
never a decimal. This is not merely observed but **guaranteed by the renderer**,
which floors the value to a whole number before formatting it (§11).

The 1% quantum is load-bearing for the control law; see `design-decisions.md`
§4. It can now be relied on rather than defended against.

Window lengths are fixed, so window start is derivable: `resets - 5h` for the
session bucket, `resets - 7d` for weekly.

---

## 3. The output format is NOT stable — two variants found in one hour

This is why every sample stores raw stdout verbatim, and why `niceclaude check`
re-parses the full history with the *current* parser rather than trusting what
was parsed at capture time.

**Variant A — no reset clause.** Whenever a window is not running:

```
Current session: 0% used
```

The reset clause is simply absent. An early parser required it and classified
this as unparseable, which would have braked hardest at the exact moment
headroom was greatest. The reset clause must be optional, and a bucket with an
unknown window start is judged against `m0`.

**This is far more common than "just after a roll", which is how it was first
recorded.** Windows are *lazily started by first use*, so an idle account has no
reset time at all — for as long as it stays idle. Measured over a 65-hour log of
3855 samples:

| Samples | `week:all models` | `session` |
|---|---|---|
| 1981 | no reset | no reset |
| 1362 | reset | no reset |
| 517 | reset | reset |

Only **13%** of the log had a live session window, and **49%** had a live weekly
window. So for most of a quiet weekend **there is no pace line to adhere to at
all** — the concept only exists once a window has been started. Anything
computing or plotting `allowed(f_t)` must treat window-inactive samples as
absent rather than as zero, or it will invent a threshold that was never real.

**Variant B — an advisory block appears.** Once there is something to report
(it showed up after subagents had run):

```
What's contributing to your limits usage?
Approximate, based on local sessions on this machine — does not include other
devices or claude.ai. Behaviors are independent characteristics, not a breakdown.
Last 24h · 48 requests · 5 sessions
Top subagents: general-purpose 3%
Last 7d · 48 requests · 5 sessions
Top subagents: general-purpose 3%
```

Not bucket data. But not everything unrecognised can be waved through either, or
the over-limit rendering (never observed — see `open-questions.md`) would be
silently ignored. Rule adopted: **lines starting with `Current` MUST parse;
anything carrying limit-ish vocabulary is surfaced loudly; everything else is
advisory.**

**Rendering jitter.** The same instant renders differently between calls:

```
resets Aug 14, 8:10pm   →   resets Aug 14, 8:09pm
resets Aug 16, 12am     →   resets Aug 15, 11:59pm
```

Both pairs are 60s apart in epoch terms. Normalize and treat sub-two-minute
differences as identical, or the stability assertion fires constantly.

**Bucket lines are variable in number** — per-model weekly lines appear only for
models actually used. Parse keyed on the label in parentheses, never by
position.

---

## 4. Bucket semantics

| Bucket | Applies to |
|---|---|
| `session` (5h) | everything |
| `week:all models` | everything |
| `week:<label>` | only the named model |

A model-scoped bucket draws on **both** its own weekly budget and the shared
one, so `week:all models` is never the whole story.

The internal bucket list, from the binary:

```
five_hour, seven_day, seven_day_oauth_apps, seven_day_opus,
seven_day_sonnet, cinder_cove, extra_usage, limits
```

**`seven_day_opus` exists** — contrary to the working assumption that only Fable
has a per-model budget. The renderer does not emit it directly; only `five_hour`,
`seven_day`, and (for `max`/`team`/null subscriptions) `seven_day_sonnet` are
hardcoded, with everything else arriving through the dynamic `model_scoped`
projection. `extra_usage` has never been observed rendered.

**The per-model label is not stable.** It comes from two different places:

- a hardcoded string `"Current week (Sonnet only)"` — note the `only` suffix;
- a server-supplied `displayName` via `model_scoped`, which is where the
  observed `"Current week (Fable)"` came from.

So the label can be `Fable`, `Sonnet only`, or anything the server decides.
Matching a declared model against it by equality is wrong and silently fails —
`week:sonnet` never equals `week:sonnet only`. Match on **whole words** within
the label instead. See `design-decisions.md` §10.

---

## 5. Hook events available

Extracted from the binary: `PreToolUse`, `PostToolUse`, `UserPromptSubmit`,
`Stop`, `SubagentStart`, `SubagentStop`, `SessionStart`, `SessionEnd`,
`PreCompact`, `PostCompact`, `Notification`.

---

## 6. Hook payload

`PreToolUse`, in the parent agent:

```json
{"session_id":"1111...","transcript_path":"/.../1111....jsonl",
 "cwd":"/path/to/project","prompt_id":"2f3afd95-...",
 "permission_mode":"default","effort":{"level":"high"},
 "hook_event_name":"PreToolUse","tool_name":"Bash",
 "tool_input":{"command":"echo hi","description":"Echo hi"},
 "tool_use_id":"toolu_019..."}
```

- **`cwd` is present** — this is what folder-scoped policy keys on.
- **The wrapper's environment is inherited** by the hook process.
- **`--session-id <uuid>` is honoured** and appears in the payload, so a launcher
  can choose the id in advance.
- **There is no `model` field**, on any event. Hence §10 in
  `design-decisions.md`.

---

## 7. Subagent behaviour — the important one

From a run that spawned parallel subagents:

```
PreToolUse    sid=001d5048…  cwd=/w  tool=Agent  agent=null
SubagentStart sid=001d5048…  cwd=/w              agent=general-purpose
PreToolUse    sid=001d5048…  cwd=/w  tool=Bash   agent=general-purpose  ← in subagent
SubagentStop  sid=001d5048…  cwd=/w              agent=general-purpose
```

- **Hooks fire inside subagents.** This is the freeze point for a fan-out.
- **`cwd` is identical** across parent, `SubagentStart`, subagent tool calls, and
  `SubagentStop`.
- **`session_id` is also uniform** across the tree.
- Subagent `PreToolUse` carries `agent_id` and `agent_type`; parent tool calls
  do not. So parent and subagent calls are distinguishable if policy ever needs
  it.

**Consequence:** a dozen subagents each hit the hook independently, each sleeps,
and the parent is already blocked on its children. The whole tree freezes with
**no coordination, no PID tracking, no signal fan-out.** Each hook asks the same
question, gets the same answer, and they converge.

---

## 8. Hook timeouts are honoured well past the default

The default is 60s; the `timeout` field is in seconds.

| Test | Configured timeout | Slept | Elapsed | Result |
|---|---|---|---|---|
| 1 | 300 | 120s | 128s | tool call proceeded normally |
| 2 | 21600 | 700s | 708s | tool call proceeded normally |

No clamp at 60s, 300s, or 600s; no independent watchdog killing long hooks. A
multi-hour freeze is not *proven*, but nothing suggests a ceiling.

Because `PreToolUse` fires between API turns, nothing is in flight during the
sleep — the freeze parks between connections rather than stalling one
mid-flight. This is why sockets are a non-issue.

---

## 9. Latency measurements

Per hook invocation, 20 iterations:

| Implementation | Avg |
|---|---|
| Bare Python interpreter | 11ms |
| **Python hook, stdlib-only module** | **16ms** |
| bash + jq hook | 16ms |
| Python via full CLI module (argparse/re/subprocess) | 29ms |
| Installed `niceclaude-hook`, end to end | 19–22ms |

`uv run` was rejected for the hot path: it re-resolves the environment on each
run, costing 100–300ms.

---

## 10. Packaging

`uv build` produces an **18KB `py3-none-any` wheel with zero dependencies**.
Universal, installable offline, works on Windows without modification.

---

## 11. How the renderer behaves (established by inspecting the shipped binary)

The most useful thing we learned, and it settles several previously open
questions. What follows is a **description of observed behaviour**, written from
our own reading — deliberately not a copy of Anthropic's source, which is theirs
and not ours to republish. In pseudocode of our own:

```
render(payload):
    if payload has no rate_limits:            emit nothing at all
    rows = [ ("Current session",            five_hour),
             ("Current week (all models)",   seven_day) ]
    if subscription is max / team / unset:
        rows += ("Current week (Sonnet only)", seven_day_sonnet)
    rows += one row per model-scoped limit, titled by a server-supplied
            display name

    for (title, limit) in rows:
        if limit missing, or its utilization is null:   skip this row entirely
        suffix = " · resets <formatted time>"   if the limit has a reset time
                 ""                            otherwise
        emit "<title>: <floor(utilization)>% used<suffix>"

    if no rows survived:                       emit nothing at all
```

Five consequences follow:

1. **There is no over-limit branch.** `utilization` is just a number; at the
   limit it renders `100% used` in exactly the same format. The "we have never
   seen what happens past the limit" worry was largely misplaced — nothing
   special happens.
2. **It cannot hang on rate limits.** This is a pure formatter over an
   already-fetched status payload. No inference call is involved, which is also
   why polling it costs no tokens.
3. **Integers by construction** — the utilization value is floored to a whole
   number before it is formatted.
4. **The reset clause is genuinely optional** — it is appended only when the
   limit carries a reset time, and omitted entirely otherwise. The
   observed `Current session: 0% used` was correct behaviour, not a glitch.
5. **Buckets vanish when their utilization is null** — the row is skipped, not
   zeroed — and the entire
   block is omitted when `rate_limits` is null (`if (!t) return null`). Parsing
   must key on labels and tolerate absence — never assume a fixed line count.

The preamble has two forms, selected by `tee().isUsingOverage`:

```
You are currently using your subscription to power your Claude Code usage
You are currently using your overages to power your Claude Code usage. We will
automatically switch you back to your subscription rate limits when they reset
```

The overage variant contains the words "rate limits", which naive
suspicious-line detection flags as an anomaly for as long as an account is on
overage billing. Both are allow-listed by prefix.

---

## 12. How to inspect the binary WITHOUT destroying the machine

The binary is ~323MB and has enormous stretches with no newline. A regex scan
over it consumed all available RAM and took down the WSL instance. Do not repeat
this:

```bash
# NEVER. Catastrophic backtracking over a multi-hundred-MB "line",
# and `sort` blocks so `head` applies no backpressure at all.
grep -aoE '.{0,110}Current (session|week).{0,110}' "$BIN" | sort -u | head -20
```

The safe procedure is two bounded steps — fixed-string search for **byte
offsets**, then seek and read a small window:

```bash
grep -abo -F "Current session" "$BIN" | head -10      # offsets only, -F is fixed-string
```

```python
pat = re.compile(rb"[ -~]{40,}")
with open(BIN, "rb") as fh:
    fh.seek(max(0, offset - 1400))
    blob = fh.read(3000)                               # bounded read
    for s in pat.findall(blob):
        print(s.decode("ascii", "replace"))
```

Rules: never regex-scan a large binary; never put `sort` between an unbounded
producer and `head`; wrap exploratory commands in `ulimit -v`.

---

## 13. Capturing the over-limit rendering from someone else's machine

We deliberately never hit a limit, so the over-limit state cannot be observed
locally. It costs the other person nothing — `/usage` is a status query, not an
inference call — so the cheapest route is to ask someone already over.

PowerShell (one block). Note `>` in Windows PowerShell writes **UTF-16LE**, which
mangles the U+00B7 separator, so `Out-File -Encoding utf8` is required:

```powershell
$f="usage_info.txt"; $v=(claude --version 2>&1|Out-String).Trim(); $t=Get-Date
$o = claude -p "/usage" 2>&1 | Out-String; $rc=$LASTEXITCODE
$ms=[int]((Get-Date)-$t).TotalMilliseconds
@("version: $v","exit: $rc","elapsed_ms: $ms","--- output ---",$o) |
  Out-File -Encoding utf8 $f
```

If it hangs instead, that is itself the answer — record how long before giving up.

**Ask them to read the file before sending it.** The advisory block can list
`Top skills`, `Top plugins` and `Top MCP servers` — internal tooling names that
should not travel into a public repo unreviewed.

---

## 14. Reported usage is only *approximately* monotonic

The design leans on usage never falling inside a window: it is what makes a
stale snapshot a usable lower bound (`design-decisions.md` §12) and what makes
the wake time solvable in closed form (§6). Measured against 4015 samples, that
holds — but not exactly.

Observed live, with an unchanged reset label:

```
14:39  Current session: 4% used · resets Aug 17, 7pm (UTC)
14:40  Current session: 4% used · resets Aug 17, 7pm (UTC)
14:41  Current session: 3% used · resets Aug 17, 7pm (UTC)   <-- fell
14:42  Current session: 6% used · resets Aug 17, 7pm (UTC)
```

Not a parse error: the raw output really says 3%. The cause is almost certainly
§11's `floor` — the renderer floors a float, so a small backend recalculation
crossing an integer boundary (4.02 → 3.98) surfaces as a whole point.

Frequency across the corpus:

| Bucket | Samples | Within-window decreases |
|---|---|---|
| `session` | 4015 | **1** (0.02%), of 1 point |
| `week:all models` | 4015 | 0 |
| `week:Fable` | 4015 | 0 |

**This does not weaken the safety argument.** The hook already compares
`pct + 1` against the line (§5 of `design-decisions.md`), so one point of
downward noise sits inside a margin that exists anyway. "Stale data is a lower
bound" survives ±1.

**It did make `check` cry wolf**, which matters more than it sounds: this was
the *only* anomaly in the entire corpus, and a checker that is routinely wrong
stops being read. `check` now tolerates a 1-point drop.

The tolerance is applied against the window's **running maximum**, not the
previous sample. Comparing to the predecessor would tolerate a run of 1-point
drops indefinitely, so a steady slide — which rounding cannot produce, and which
would indicate a real parser fault — could walk down one point at a time
unnoticed.
