# Windows results

First execution of `niceclaude` on Windows. Everything below was run on a real
workstation against a real, authenticated Claude Code install — not a CI runner.

**Headline: three bugs, all Windows-or-locale specific, all invisible on Linux.
Two of them silently un-paced every folder.** The critical question — does
Claude Code invoke the hook synchronously and block on it — is **answered yes**,
and the freeze mechanism is sound. But it never fired at all until the hook
command was made shell-safe, because Claude Code runs hook commands through Git
Bash, where the backslashes in a Windows path are escape characters.

All three are fixed in this commit, with regression tests. Test suite is now
**96 tests** (was 86): 95 passed + 1 skipped on Windows, 96 passed on Linux.
(The checks below were run against the 80-test tree this session started on;
the baseline moved to 86 upstream while the work was in progress.)

---

## 0. Version banner

```
claude --version            2.1.228 (Claude Code)
$PSVersionTable.PSVersion   5.1.19041.7663
OSVersion                   10.0.19045.0        (Windows 10 Pro 19045)
uv --version                uv 0.11.21 (5aa65dd7a 2026-06-11 x86_64-pc-windows-msvc)
python                      3.13.7 (tags/v3.13.7:bcee1c3, Aug 14 2025) [MSC v.1944 64 bit (AMD64)]
locale.getencoding()        cp1252
[Console]::OutputEncoding   UTF-8 (65001)
tzutil /g                   Eastern Standard Time  (America/New_York, EDT/UTC-4 on the test date)
zoneinfo.available_timezones()   0 entries — Windows ships no tz database
```

Date of run: 2026-08-17. Shell: PowerShell 5.1. Machine profile path contains no
space (`C:\Users\Joshu`), so the spaced-path cases were constructed explicitly.

---

## Summary table

| # | Check | Result |
|---|---|---|
| 1 | Install | **PASS** (one cosmetic note) |
| 2 | Automated suite | **PASS** — 80/80 pre-fix, 96 post-fix (after rebase) |
| 3 | Hot-path latency | **PASS with a caveat** — ~102ms vs 19–22ms on Linux |
| 4 | **Does Claude Code block on the hook?** | **FAIL → root-caused → FIXED → PASS** |
| 5 | Paths with spaces | **FAIL (same root cause) → FIXED → PASS** |
| 6 | Daemon lifecycle | **PASS** (stale pidfile, as predicted) |
| 7 | Data source and parser | **FAIL ×2 → FIXED → PASS** |
| 8 | Path edge cases | **PASS**, with one documented limitation (UNC) |
| 9 | Plotting | **PASS** |

---

## The three bugs

### Bug 1 — the hook command is destroyed by Git Bash (hard blocker)

**Claude Code runs hook commands through a POSIX shell (Git Bash) on Windows.**
In that shell a backslash is an escape character, so the native path
`niceclaude install` wrote —

```
C:\Users\Joshu\.local\bin\niceclaude-hook.EXE
```

— had its separators eaten before exec. Nothing errored, nothing logged, and
`claude` sailed straight through every tool call. **The folder reported itself
paced while being completely ungoverned** — the exact failure mode the handoff
names as the worst this tool has.

**Evidence.** A marker hook writing to `C:\ncwork\M-user.txt` produced, in the
process's working directory, a file named:

```
name  : CncworkM-user.txt
chars : U+0043 U+F03A U+006E U+0063 U+0077 U+006F U+0072 U+006B U+004D ...
                 ^^^^^^ MSYS private-use mapping for ':'
```

Both backslashes gone; the illegal `:` remapped so the file could be created.

Confirmed independently — a hook command of `echo FIRED > /c/ncwork/M-posix.txt`
created `C:\ncwork\M-posix.txt`. Only a POSIX shell with MSYS path translation
does that.

Command-form matrix, all else identical:

| command | hook ran? |
|---|---|
| `C:\ncwork\marker.cmd` | no |
| `cmd.exe /c echo FIRED >> C:\ncwork\M-star.txt` | no |
| `cmd.exe /c echo FIRED > C:\\ncwork\\M-dbl.txt` | no |
| `cmd.exe /c echo FIRED > C:/ncwork/M-fwd.txt` | **yes** |
| `echo FIRED > /c/ncwork/M-posix.txt` | **yes** |

Not the matcher: `"*"`, `""`, `"Bash"` and an omitted matcher all behaved
identically. Not the scope either — `--settings`, project `.claude/settings.json`
and user `~/.claude/settings.json` all failed the same way. The hooks *were*
firing in every scope the whole time; only the command string was broken.

**Fix** — `cmd_install` now emits forward slashes on Windows
([cli.py:466](../src/niceclaude/cli.py#L466)). Forward slashes are accepted by
the Windows API and need no escaping, so the command works whether the shell is
`sh` or `cmd.exe`. The existing space-quoting is unchanged and still applies.

```
hook:     C:/Users/Joshu/.local/bin/niceclaude-hook.EXE
```

### Bug 2 — `/usage` decoded with the locale encoding, not UTF-8

`subprocess.run(..., text=True)` with no explicit `encoding` decodes with
`locale.getencoding()`, which on Windows is the ANSI code page — **cp1252** here,
not UTF-8. `/usage` separates its fields with U+00B7, emitted as UTF-8 `C2 B7`;
read as cp1252 that becomes `Â·`, `LINE_RE` stops matching, and the line is lost.

Raw bytes, captured directly:

```
RAW BYTES : b'Current session: 3% used \xc2\xb7 resets Aug 17, 2:59pm (America/New_York)'
cp1252    : 'Current session: 3% used Â· resets Aug 17, 2:59pm (America/New_York)'
utf-8     : 'Current session: 3% used · resets Aug 17, 2:59pm (America/New_York)'
```

Pre-fix `niceclaude sample` output (exit 1, `parse_ok: false`):

```json
"buckets": { "week:Fable": { ... } },
"unparsed_lines": [
  "Current session: 3% used \u00c2\u00b7 resets Aug 17, 2:59pm (America/New_York)",
  "Current week (all models): 1% used \u00c2\u00b7 resets Aug 22, 7:59pm (America/New_York)"
]
```

Only `week:Fable` survived — because it is the one line carrying no separator.

**Why this matters more than a parse warning.** The published snapshot then held
no `session` and no `week:all models`. For any folder declaring `--model opus`
or `--model sonnet`, `decide()` finds `enforced` empty and returns *"no usable
buckets in snapshot"* — a permanent **blind brake**. Observed live:

```
2026-08-17T14:42:37Z brake  cwd=c:\ncwork\test [PreToolUse] no usable buckets in snapshot
```

The fail-safe direction held (it braked rather than sailed through), but the
tool was unusable: every paced folder would freeze forever on a healthy account.

A second consequence: the corruption happens **at capture**, so `usage.jsonl`
stores mojibake in the `raw` field. The "raw is stored verbatim so the parser
stays re-runnable" guarantee is void for anything recorded pre-fix — those
samples can never be re-validated.

**Fix** — explicit `encoding="utf-8", errors="replace"`
([cli.py:244](../src/niceclaude/cli.py#L244)). `errors="replace"` keeps a
malformed read surfacing as an unparsed line rather than throwing away the whole
sample.

### Bug 3 — the reset clause is read as UTC when it is local

Two defects, both masked on Linux because the dev box was set to UTC.

**3a. The timezone pattern could not match an IANA name.** `(?P<tz>[A-Z]{2,5})`
matches `UTC`; it cannot match `America/New_York` (too long, contains `/` and
`_`). The optional reset group failed, so the whole line failed. This is a
second, *independent* cause of the same missing-buckets symptom as Bug 2 — with
the encoding fixed alone, the session and week lines still did not parse:

```
NOMATCH 'Current session: 4% used · resets Aug 17, 2:59pm (America/New_York)'
NOMATCH 'Current week (all models): 1% used · resets Aug 22, 7:59pm (America/New_York)'
MATCH   'Current week (Fable): 0% used'
```

**3b. The parsed time was stamped UTC regardless of the zone printed beside it.**
`dt.replace(tzinfo=timezone.utc)` was correct only on a UTC machine. Here it put
every reset **four hours early**, and the error runs in the **fail-open**
direction — the one direction this tool must never fail in:

| | reset epoch | window start | f_t at 15:00Z | pace line |
|---|---|---|---|---|
| old (stamped UTC) | 14:59Z | 09:59Z | **~99%** | permits almost everything |
| fixed (EDT = UTC-4) | 18:59Z | 13:59Z | **20.1%** | correct |

Inflating f_t raises `allowed(f_t)`, which permits spending that should have
braked.

**Proved empirically, not inferred.** If `2:59pm` meant UTC the session window
would have rolled at 14:59Z. The daemon sampled straight through that instant:

```
14:57:14Z | Current session: 7% used · resets Aug 17, 2:59pm (America/New_York)
14:58:14Z | Current session: 8% used · resets Aug 17, 2:59pm (America/New_York)
14:59:30Z | Current session: 9% used · resets Aug 17, 2:59pm (America/New_York)
15:00:09Z | Current session: 10% used ...
```

No reset, usage still climbing, and the local wall clock read 10:58. The label
is local time.

**Fix** — widen the pattern to `(?P<tz>[^)]+)` (the same idiom the bucket label
already uses), and add `resolve_tz()`
([cli.py:113](../src/niceclaude/cli.py#L113)):

- `UTC`/`GMT`/`Z` → UTC, exactly as before, so every existing test is unchanged.
- otherwise try `zoneinfo` — exact wherever a tz database exists (Linux, macOS).
- Windows ships **no** tz database (`available_timezones()` returned 0 entries;
  `ZoneInfo("America/New_York")` raises `ZoneInfoNotFoundError`), and this
  project is deliberately dependency-free, so fall back to reading the time as
  **local**, via `astimezone()` on a naive datetime. That applies the OS's own
  DST rules for the date in question.

**Assumption worth a maintainer's eye:** the fallback relies on the renderer
printing the machine's *own* zone. Both data points agree (a UTC box reports
`UTC`; this box reports its own `America/New_York`), and it is the obvious
implementation — but it is an inference from two samples, not a documented
contract. If it is ever wrong, the failure returns to the fail-open direction.
Adding `tzdata` as a Windows dependency would remove the inference entirely, at
the cost of the zero-dependency property; that is a design call, not a bug fix,
so it is flagged rather than taken.

---

## Check-by-check

### 1. Install — PASS

`uv tool install ".[plot]"` (the local repo — `niceclaude` is not on PyPI, so the
handoff's `uv tool install "niceclaude[plot]"` cannot work as written).

- [x] `install` **found** `niceclaude-hook.exe` — via PATH, at
      `C:\Users\Joshu\.local\bin\niceclaude-hook.exe`. Both console scripts
      installed there.
- [x] `settings.json` written to `%APPDATA%\niceclaude` ✓
- [x] `policy.json` written to `%LOCALAPPDATA%\niceclaude` ✓
- [x] Quoting for spaces — verified end-to-end, see check 5.

Cosmetic note: `shutil.which` returns the PATHEXT casing, so the recorded
command reads `niceclaude-hook.EXE`. Harmless — Windows paths are
case-insensitive, and `norm_path` lowercases anyway.

### 2. Automated suite — PASS

```
80 passed in 0.43s        (pre-fix, on the tree this session started from)
95 passed, 1 skipped      (post-fix, on Windows, rebased onto the 86-test tree)
```

No path test failed. `normcase`/`normpath`/`realpath` behaved correctly
throughout. The one skip is `test_iana_names_resolve_where_a_tz_database_exists`,
which is skipped precisely because Windows has no tz database — it runs on Linux.

`tests/smoke_installed.py` also passes all 13 checks against the installed entry
points, including the new forward-slash command.

### 3. Hot-path latency — PASS with a caveat

Linux: 19–22ms. Windows, measured 20 invocations at a time:

| | ms/invocation |
|---|---|
| `cmd.exe /c exit` (PowerShell spawn floor) | 24.3 |
| `python -c pass` (tool venv) | 64.8 |
| `python -m niceclaude.hook` | 76.9 |
| **`niceclaude-hook.exe`** | **115.6 / 121.1 / 105.9** |

Net of the ~24ms measurement floor, the real cost is **~82–98ms**, about 4–5×
Linux. It is *not* the hundreds of milliseconds the handoff set as the alarm
threshold, so no action taken.

Where it goes: ~40ms is Windows Python interpreter startup, ~12ms is the module
itself, and **~49ms is the console-script `.exe` launcher**, which spawns the
interpreter as a child process. The handoff's suggested fallback — a generated
`.cmd` shim invoking the venv interpreter directly — would recover roughly that
49ms. Worth doing only if the hook's cost ever becomes visible; recorded here so
the number is known.

### 4. THE critical test — PASS (after Bug 1)

**Pre-fix: it sailed straight through.** `claude -p` completed in 15.0s, printed
`WINTEST`, and no `hook.log` was created at all.

Ruling out the obvious confounds before blaming the hook — via
`--output-format stream-json --verbose`:

```
system: cwd=C:\ncwork\test              <- correct, and paced
TOOL_USE: Bash -> {"command":"echo VA2","description":"Echo VA2"}
TOOL_RESULT: VA2                        <- a PreToolUse event really did occur
```

And the hook itself was fine in isolation — invoked directly with the same
payload it blocked past 10s and logged a brake. So the hook worked, the event
occurred, and Claude Code still ran nothing. That is Bug 1.

**Post-fix, with the command Claude Code can actually exec:**

- [x] Did the `claude` process actually freeze? **Yes** — still running at 120s.
- [x] Does `hook.log` contain a `brake` line naming the folder? **Yes.**
- [x] After `off`, does it release **and** log a matching `release`? **Yes**,
      8.1s after `off` — within one 15s chunk.
- [x] Does the tool call then complete normally? **Yes** — `WINTEST4`.

```
2026-08-17T14:52:47Z brake  cwd=c:\ncwork\test [PreToolUse] no usable buckets in snapshot
2026-08-17T14:55:02Z release cwd=c:\ncwork\test after 135s (unpaced)
```

**Claude Code invokes the hook synchronously and blocks on it on Windows. The
freeze mechanism holds, and the design does not need rethinking.**

One new operational fact: the `timeout` in `settings.json` is enforced. An early
run of this test used `"timeout": 60` and Claude Code killed the hook at 60s and
proceeded — leaving a `brake` with **no matching `release`**, because the hook
process was terminated before it could log one. So an unmatched brake in
`hook.log` means "hook timed out", not necessarily "hang". `install` writes
21600, so this only bites a hand-written fragment.

### 5. Paths with spaces — PASS (after Bug 1)

Both halves, post-fix. Note this was never a *quoting* bug — the space handling
was already correct; the backslashes broke it first.

**Paced folder with a space** — `C:\ncwork\My Projects\test`, froze as expected,
with a genuine pace-line brake rather than a blind one:

```
2026-08-17T15:00:49Z brake  cwd=c:\ncwork\my projects\test [PreToolUse] session 10.0% over line 0.2%; week:all models 2.0% over line 0.2%
2026-08-17T15:02:34Z release cwd=c:\ncwork\my projects\test after 105s (unpaced)
```
`SPACETEST` completed after release.

**Hook executable under a spaced path** — exercising the quoting `cmd_install`
applies. Command under test, exactly the form `install` now emits:

```
"C:/ncwork/spaced dir/niceclaude-hook.exe"
```

Froze at 75s, released on `off`, `SPACEDEXE` completed.

### 6. Daemon lifecycle — PASS

- [x] Second start refused: `error: daemon already running (pid 11960);
      niceclaude stop first`, exit code **1**.
- [x] `stop` killed it — zero `niceclaude` processes left afterwards.
- [ ] `daemon.pid` **is left behind**, as the handoff predicted. `taskkill /F`
      is SIGKILL-like: the `SIGTERM` handler that fixed this on Linux cannot
      help, since Windows has no equivalent to deliver. `read_pid()`
      liveness-checks, so a second `stop` correctly reports `no daemon running`
      and a subsequent `watch` starts fine. Only a recycled PID would bite.
      **Expected-but-recorded, as instructed — not treated as a bug.**

### 7. Data source and parser — PASS (after Bugs 2 and 3)

- [x] Does `claude -p "/usage"` work, and how long? **Yes**, 3.4–4.8s
      (`elapsed_ms` 3444–4613).
- [x] Does `·` survive the pipe? **No, pre-fix** — and the handoff's instinct was
      exactly right: a console-encoding problem, not a regex problem. See Bug 2.
      Post-fix the separator arrives intact (`\u00b7`), as does the em-dash in
      the advisory block (`\u2014`).
- [x] Does `check` report `no anomalies`? **Yes**, over post-fix samples:
      `2 samples over 0.0h; buckets: ['session', 'week:Fable', 'week:all models']`
      / `no anomalies`.
- [x] Do the bucket labels differ from Linux? **Yes** — this account has a
      `week:Fable` per-model bucket, and the timezone renders as an IANA name
      rather than `UTC`.

Raw output, verbatim, post-fix:

```
You are currently using your subscription to power your Claude Code usage

Current session: 4% used · resets Aug 17, 2:59pm (America/New_York)
Current week (all models): 1% used · resets Aug 22, 7:59pm (America/New_York)
Current week (Fable): 0% used

What's contributing to your limits usage?
Approximate, based on local sessions on this machine — does not include other devices or claude.ai. Behaviors are independent characteristics, not a breakdown.

Last 7d · 1453 requests · 13 sessions
  70% of your usage was at >150k context
  22% of your usage came from subagent-heavy sessions
  16% of your usage came from sessions active for 8+ hours
  Top skills: /claude-api 1%
  Top subagents: workflow-subagent 17%
  Top MCP servers: jl_pdf_mcp 1%
```

`niceclaude status` post-fix, showing the control law working:

```
  session                 10% used | line   0.2% |  20.1% elapsed | ENFORCED  <-- OVER
  week:all models          2% used | line   0.2% |  23.2% elapsed | ENFORCED  <-- OVER
  week:Fable               0% used | line   0.0% (floor) | window start unknown | ignored
```

20.1% elapsed of a 5h window ≈ 1h, matching a window that began at 13:59Z.

### 8. Path edge cases — PASS, one documented limitation

- [x] **Case insensitivity.** Paced `C:\NCWork\Test`, queried `c:\ncwork\test` —
      matched. This is what `normcase` is for, and it works.
- [x] **Mapped drive.** No network share was available, so a `subst Z: C:\ncwork`
      virtual drive was used. `niceclaude status Z:\test` resolved through the
      mapping to `c:\ncwork\test` and matched the rule — `realpath` follows
      `subst`. That is the desirable behaviour: one directory, one policy.
- [x] **UNC path.** `norm_path` does **not** throw — the handoff's worry about
      `realpath` was unfounded:
      ```
      '\\localhost\C$\ncwork\test'  -> '\\localhost\c$\ncwork\test'
      '\\localhost\C$\NCWork\Test'  -> '\\localhost\c$\ncwork\test'
      ```
      Case folds correctly and the UNC form is preserved.

      **Limitation, not fixed:** a UNC path and its local equivalent are
      *different policy keys*. `realpath` does not resolve `\\localhost\c$\...`
      back to `c:\...`, so pacing `C:\proj` does not govern an agent whose cwd is
      `\\server\share\proj`, and vice versa. For a genuinely remote share there
      is no local equivalent and the question is moot; the gap only matters for
      loopback/admin shares. Any fix would mean maintaining a UNC↔local mapping,
      which is fragile — recording it instead.

### 9. Plotting — PASS

`niceclaude plot -o usage.png` — matplotlib 3.11.1 installed and rendered a
139,069-byte PNG under the forced `Agg` backend, no display required.

```
session                0/7 live samples over the line (0.0%), worst overshoot 0.0 pts
week:all models        0/7 live samples over the line (0.0%), worst overshoot 0.0 pts
```

(`0% over the line` is the documented non-issue, not a finding.)

---

## Known non-issues — confirmed, not chased

All three held on Windows and were left alone:

1. `0% of samples above the line` — expected; appeared in check 9 as predicted.
2. `resets_at` missing for most samples — `week:Fable` carried no reset clause
   throughout, exactly as described.
3. The advisory block — correctly classified as informational; it appeared in
   every sample and never counted as a parse failure.

---

## Changes made

Three fixes, all in [src/niceclaude/cli.py](../src/niceclaude/cli.py), plus
[tests/test_windows_regressions.py](../tests/test_windows_regressions.py)
(10 new tests). No test was relaxed; the suite grew 86 → 96.

| Bug | Fix |
|---|---|
| 1 | `cmd_install` converts the hook path to forward slashes on Windows |
| 2 | `sample_once` pins `encoding="utf-8", errors="replace"` |
| 3a | `LINE_RE` timezone group widened to `[^)]+` |
| 3b | new `resolve_tz()`; `parse_reset` converts from the named zone |

Bugs 2, 3a and 3b are **not** Windows-specific in principle — 3a and 3b affect
any machine not set to UTC, and 2 affects any non-UTF-8 locale. They surfaced
here only because this was the first non-UTC, non-UTF-8 machine to run the tool.

## Machine state left behind

- `niceclaude` installed as a uv tool; hook installed at
  `%APPDATA%\niceclaude\settings.json`. **No global hook** — `~/.claude/settings.json`
  was temporarily patched during diagnosis and has been restored to its original
  contents (backup kept at `~/.claude/settings.json.bak-niceclaude-validation`).
- `policy.json` holds three rules, all `paced: false`.
- `C:\ncwork\` holds the scratch folders and the settings fragments used for the
  command-form matrix.
- `%LOCALAPPDATA%\niceclaude\usage.jsonl` contains **21 pre-fix samples with
  mojibake `raw`**, so `niceclaude check` reports 40 unparsed-line problems
  against them. They cannot be repaired — the corruption is at capture. Delete
  the file to reset, or keep it as a record of the bug.
- `daemon.pid` is present but stale (see check 6); harmless.

## Open questions for the maintainer

1. **The local-timezone inference in `resolve_tz`'s fallback** — see Bug 3.
   Worth confirming against a machine whose Claude Code reports a zone it is not
   actually in, if such a configuration exists.
2. **Whether to ship the `.cmd` shim** for the ~49ms launcher overhead (check 3).
3. **`design-decisions.md` §8** ("install as a `--settings` fragment") is
   unaffected in its reasoning, but §7 (path handling) should probably gain a
   note that the hook command is shell-interpreted by Git Bash on Windows —
   that constraint is now load-bearing and is not obvious from the code.
