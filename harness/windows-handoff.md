# Windows handoff

**For an agent picking this up on Windows.** Everything in this project has been
built and verified on Linux. Not one line has executed on Windows. The code is
written to be portable, but "written to be portable" and "verified" are different
things, and this file is the gap between them.

Read `harness/README.md` first for what the tool is and how it fits together,
then `design-decisions.md` §7 (path handling) and §14 (daemon lifecycle), which
are where the Windows-specific risk concentrates.

**CI already covers part of this.** `.github/workflows/tests.yml` runs the unit
suite and `tests/smoke_installed.py` on `windows-latest`, which exercises the
console scripts, `install`, real filesystem path matching, an actual brake, and
the kill switch. Check the latest run before doing anything by hand — if it is
green, steps 1, 2, 3, 6 and 8 are largely done for you.

What CI **cannot** cover is anything needing Claude Code installed and
authenticated: **step 4 is the one that matters and only a real machine can do
it.** Steps 5 and 7 also need a real install.

**Record your results in `harness/windows-results.md`** (create it), with the
version banner from step 0 at the top. Where something fails, capture the exact
output — a paraphrase is not useful to whoever fixes it.

---

## 0. Version banner — capture this first

```powershell
claude --version
$PSVersionTable.PSVersion
[System.Environment]::OSVersion.Version
uv --version
uv run --no-project python -c "import sys; print(sys.version)"
```

Everything below is version-sensitive; findings without this banner can't be
compared to anything later.

---

## 1. Install

```powershell
uv tool install "niceclaude[plot]"
niceclaude install
```

Check and record:

- [ ] Does `install` **find `niceclaude-hook.exe`**? It looks on PATH first, then
      beside `sys.argv[0]`. If it reports "could not find the niceclaude-hook
      executable", note where `uv` actually put the two `.exe` files.
- [ ] Does it write `settings.json` under **`%APPDATA%\niceclaude`**? (Config and
      data deliberately live in different places — `design-decisions.md` §16.)
- [ ] Does `policy.json` land under **`%LOCALAPPDATA%\niceclaude`**?
- [ ] Is the `command` in `settings.json` **quoted** if its path contains a space?

---

## 2. The automated suite

```powershell
uv run --with pytest pytest tests/ -q
```

Expected: **60 passed**. The path tests are the likeliest failures — they
exercise `normcase`/`normpath` behaviour that differs on Windows (case
insensitivity, backslash separators, drive letters).

If any fail, record the full failure. Do **not** relax the test to make it
pass — a path test failing on Windows probably means `resolve()` would silently
mismatch a real folder, which is the worst failure mode this tool has (a folder
that looks paced but isn't).

---

## 3. Hot-path latency

```powershell
$p = '{"cwd":"C:\\Users\\Public","hook_event_name":"PreToolUse"}'
Measure-Command { 1..20 | % { $p | niceclaude-hook } }
```

Linux measures **19–22ms** per invocation. Record the Windows figure. It runs on
every tool call in every agent and subagent, so hundreds of milliseconds would
be a real problem — if it is bad, the fallback is a generated `.cmd` shim that
invokes the tool venv's interpreter directly, skipping the console-script
launcher.

---

## 4. THE critical test — does Claude Code invoke the hook at all?

Everything else is cosmetic next to this. On Linux the hook is invoked
synchronously and Claude Code *blocks* on it; that blocking is the entire freeze
mechanism. It is not documented behaviour and must be confirmed, not assumed.

```powershell
mkdir C:\ncwork\test
niceclaude on C:\ncwork\test --model opus --m0 0 --m1 99   # line ~1%: unreachable
niceclaude watch          # leave running in a second terminal
cd C:\ncwork\test
claude -p "Run one bash command: echo WINTEST. Then stop." `
  --settings "$env:APPDATA\niceclaude\settings.json" --allowedTools "Bash"
```

Expected: **it hangs.** Give it 60 seconds, then in another terminal:

```powershell
Get-Content "$env:LOCALAPPDATA\niceclaude\hook.log"
niceclaude off C:\ncwork\test        # releases within one chunk (~15s)
```

Record:

- [ ] Did the `claude` process actually freeze, or sail straight through?
- [ ] Does `hook.log` contain a `brake` line naming the folder?
- [ ] After `off`, does it release **and** log a matching `release` line?
- [ ] Does the tool call then complete normally (`WINTEST` in the output)?

**A brake with no matching release, or no freeze at all, is a hard blocker.** If
it sails through, find out how Claude Code runs hook commands on Windows —
directly, or via `cmd.exe` — and whether the command string in `settings.json`
survives that.

---

## 5. Paths with spaces

The single most likely Windows-specific breakage, because it fails *silently*:
the hook simply never runs and the folder looks paced while being completely
ungoverned.

```powershell
mkdir "C:\ncwork\My Projects\test"
niceclaude on "C:\ncwork\My Projects\test" --model opus --m0 0 --m1 99
cd "C:\ncwork\My Projects\test"
claude -p "Run one bash command: echo SPACETEST. Then stop." `
  --settings "$env:APPDATA\niceclaude\settings.json" --allowedTools "Bash"
```

- [ ] Does it freeze, as in step 4?

Also worth trying if the tool itself installed under a spaced path (e.g. a
profile directory with a space) — that exercises the quoting `cmd_install`
applies to the hook command.

---

## 6. Daemon lifecycle

```powershell
niceclaude watch                       # terminal A
niceclaude watch                       # terminal B: must REFUSE, "already running (pid N)"
Get-Content "$env:LOCALAPPDATA\niceclaude\daemon.pid"
niceclaude stop                        # terminal B
```

- [ ] Is the second start refused?
- [ ] Does `stop` actually kill it? (Windows takes the `taskkill /PID /F` path.)
- [ ] Is `daemon.pid` **removed** afterwards? On Linux this needed an explicit
      signal handler; `taskkill /F` is closer to `SIGKILL` and may well leave the
      file behind. If it does, that's expected-but-worth-recording — `read_pid()`
      liveness-checks, so it only bites on a recycled PID.

---

## 7. Data source and parser

```powershell
niceclaude sample
niceclaude check
niceclaude status .
```

- [ ] Does `claude -p "/usage"` work at all, and how long does it take?
- [ ] Does the `·` separator (U+00B7) survive the pipe intact? A parse failure
      here is almost certainly a console-encoding problem, not a regex problem —
      check `[Console]::OutputEncoding`.
- [ ] Does `check` report `no anomalies`?
- [ ] Do the per-model bucket labels differ from Linux? Record the raw output
      verbatim either way.

---

## 8. Path edge cases

- [ ] A folder on a **mapped network drive** (`Z:\...`) — does `niceclaude status`
      resolve and match it?
- [ ] A **UNC path** (`\\server\share\proj`) — `realpath` behaviour here is
      unknown and may throw.
- [ ] Case: pace `C:\NCWork\Test`, then `cd c:\ncwork\test` and check
      `niceclaude status .` still reports it paced. This is what `normcase`
      exists for.

---

## 9. Plotting (low priority)

```powershell
niceclaude plot -o usage.png
```

- [ ] Does matplotlib install and render? `Agg` backend is forced, so no display
      is needed.

---

## Known non-issues — don't chase these

- **`0% of samples above the line` is expected**, not a bug. Utilization has
  never approached the pace line in any recorded run; see `open-questions.md` §8.
- **`resets_at` missing for most samples is expected.** Windows are lazily
  started by first use, so an idle account has no reset time — 87% of the Linux
  log had no live session window. See `platform-findings.md` §3.
- **The advisory block ("What's contributing to your limits usage?") is not a
  parse failure.** It is deliberately classified as informational.
