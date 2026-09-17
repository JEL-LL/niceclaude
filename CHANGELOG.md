# Changelog

Notable changes to niceclaude, newest first.

The version in `pyproject.toml` is the only place the number is stored, and a
release is a tag push that `publish.yml` refuses if the tag disagrees with it.
Nothing enforces that a release was *described*, though, so the convention is:
whenever you change that line, add a section here in the same commit.

Entries are for people deciding whether to upgrade. Behaviour changes,
especially anything that alters whether a folder is paced, come first;
refactors and internal cleanups do not need a line at all.

## Unreleased

### Added

- **A second pace line.** `--band PCT` draws a *throttle line* that far under
  the brake line, and a hold now runs down to *it* rather than to the brake
  line. The brake line is unchanged and still means "never above this"; all of
  this happens below it.

  What it fixes: a hold used to end the moment the line reached `pess = pct+1`,
  so the agent resumed with under one quantum of headroom and the next 1% tick
  put it over again. On the weekly line that is a 1.68h hold for every 1% of
  budget, and every one of those holds is long enough to kill the prompt cache
  — so each 1% cost a full cold context re-read. A band means one hold buys a
  whole band of running instead of one tick.

  `--band-delay SECONDS` is the second, independent knob: what one hold costs
  *inside* the band. Left unset the band is pure release hysteresis and is run
  through at full speed. Set, it becomes a lower gear — each tool call holds
  that long, so the brake line is approached slowly and often not reached.
  `--no-band-delay` writes an explicit `null`, so it also overrides a value set
  in `defaults`.

  `--band 0` is the default and is the previous behaviour exactly, so upgrading
  changes nothing until you opt in.

  This does **not** replace `--max-delay`, which keeps its exact old meaning.
  The two now divide the work honestly: `max_delay` buys cache warmth by
  spending the ceiling — it is still the one knob that proceeds while over the
  line, and still how you end up hitting your head — while the band buys the
  same warmth out of headroom you had not yet spent. Inside the band the cap is
  `min(band_delay, max_delay)`; above the brake line only `max_delay` applies.

  Sizing: a band costs its width in hold time, ~1.94h per point on the weekly
  line. That is time not working, so keep it small — but it is no longer near
  the hook's registered ceiling, which was raised in the same release (below).
  `niceclaude plot --days 7 --band 2` draws a band against a log you already
  have, without touching any policy, which is the cheap way to size one.

  `status` grew a `THROTTLED` state alongside `BRAKED`, and band holds are
  logged under a `throttle` verb rather than `brake`, so they do not swamp
  `hook.log` and `grep ' brake '` still counts real brakes.

  A hand-edited `policy.json` can no longer un-pace a folder by accident.
  `"band": "2"` — a quoted number, the obvious slip — used to raise out of the
  pace arithmetic, and since the hook fails open by design the folder then ran
  *completely unpaced on every tool call* while `status` went on calling it
  paced. Every numeric knob (`m0`, `m1`, `band`, `band_delay`, `chunk`,
  `max_delay`, `fanout_reserve`) is now coerced and falls back to its default
  instead, NaN and infinity included — a NaN margin would not have crashed, it
  would have quietly read as "never over the line".

  While at or above the throttle line the hook also re-reads `/usage` once per
  invocation rather than trusting a snapshot up to `MAX_STALE` old. Releasing
  every `band_delay` to take a step, then trusting three-minute-old data
  through several of them, is how you sail past the brake line unseen. `/usage`
  costs no tokens, so this costs wall clock only, and only near the line.
- `niceclaude plot --days N` plots only the tail of the log -- `--days 7` for
  the last week, `--days 30` for the last month, fractions allowed. The whole
  log stays the default, but on a long-running daemon that grows unreadable: a
  month holds over a hundred five-hour session windows on one axis. The count
  runs back from the newest sample rather than from now, which is the same
  instant while `watch` is running and the more useful of the two once a log
  has gone stale -- `--days 7` on a log that stopped a month ago draws that
  log's last week instead of an empty figure. Clipping happens before parsing,
  so `--days 1` reads 82 records rather than 10856. Zero, negative and
  non-numeric values are refused by the parser instead of quietly drawing
  nothing.
- `niceclaude plot` now draws the per-model weekly window -- `week:Fable` and
  the like -- as its own panel, and as a third trace on the overlay. It was
  parsed and collected all along, then dropped on the way to the figure,
  because the panel list was hardcoded to session and `week:all models`. That
  is the omission most likely to mislead: on the 26-day log this was found on,
  `week:all models` never once crossed the line while `week:Fable` -- the
  bucket governing the same work -- was above it 46% of the time and overshot
  by 35 points. The plot showed two clean panels and no sign of the only
  window in trouble. The bucket is matched by shape rather than by name, since
  the label is a server-supplied display name, so a `week:Sonnet only` account
  gets the same panel without a code change.
- `niceclaude help <command>` prints a full page for one command: what it does
  and what it reads and writes, every argument with its default, and examples.
  It is the same page as `<command> --help`, which until now listed most flags
  with no explanation at all. `niceclaude help` alone prints the overview.
- `niceclaude status` now answers "how long, and which line". Each usage bucket
  gets the wait it would impose (`HOLDS 1h44m`), and the summary says what the
  hook would decide right now: the reason, the release time, and the re-check
  interval. A braked agent is otherwise indistinguishable from a hung one.
- Every bucket is priced, including the ones the folder does not enforce, which
  is what makes `--enforce` a decision you can check rather than guess at: the
  weekly line rises at 0.60 %/h against the session line at 20 %/h, so an
  `ignored` row reading in days is worth seeing.
- `--max-delay` caps how long a single brake may hold, in seconds. Over the
  line, the hook holds that long, releases while still over, and brakes again at
  the next tool call — so the restraint is applied as many short holds rather
  than one long one. The point is the prompt cache: a hold that outlives its TTL
  makes the next turn re-read the whole context from cold, so a wait taken to
  save budget can cost more than it saved. Off by default; this is the one
  setting that deliberately proceeds while over the line. Releases are logged as
  `max_delay-release`, distinct from `line-caught-up`. `--no-max-delay` removes
  a cap, writing an explicit `null` so that it also overrides one set in
  `defaults`.

### Fixed

- **The registered hook timeout was leaking budget, every six hours, silently.**
  `install` registered the hook at `timeout: 21600`. That is the real ceiling on
  any hold: when it expires the harness cancels the hook, and for `PreToolUse`
  the tool call then proceeds as though the hook had produced no decision. The
  killed process never reaches the release line in `main()`, so **nothing is
  logged** — an unmatched `brake` in `hook.log` is the entire trace.

  A weekly bucket can easily solve to a hold longer than six hours, so a folder
  far over its weekly line took one unpaced step every six hours, indefinitely.
  Found in a real log: 198 unmatched brakes, the recent ones spaced 6.00–6.01h
  apart to the second, all `week:Fable`, the percentage climbing 41 → 67 straight
  through them. Note where it bit hardest — the folder was configured
  `max_delay: null`, an explicit "never proceed while over the line", and the
  registered timeout was quietly rewriting that to `max_delay: 21600`. The knob
  asking for the most restraint got the least.

  Now `172800` (48h), as `_shared.HOOK_TIMEOUT`, with the three call sites that
  had the literal collapsed onto it and a test pinning it. A hold is bounded by
  its window's own reset, so the true worst case is one window — seven days —
  and `604800` would put the ceiling out of reach altogether.

  **Existing installs must re-run `niceclaude install`**, and that would not have
  worked either: the timeout was only rewritten inside the branch handling a
  *changed command path*, so a timeout-only change could never reach anyone
  already registered. `install` would find the command correct, print
  `(already registered)`, and leave the old ceiling in force. The timeout is now
  reconciled independently of the command.

  Do not remove the field to lift the limit: omitted, it reverts to the hook
  default — documented as 600s — which is far worse than the ceiling it replaces.
- `niceclaude status` marked every enforced bucket `ENFORCED` and printed a hold
  time even for a folder switched off with `niceclaude off`, or with
  `global off` in force. The verdict line below it correctly said `not paced`,
  so the table contradicted the verdict — and the table is the part that gets
  scanned. Such rows now read `ignored` / `would hold`, with a line saying which
  switch is off. Behaviour was always correct; only the report was wrong.

### Changed

- The pace-line arithmetic moved to `_shared.bucket_pace`, and both the hook and
  `status` now use it. `status` had its own copy, which was free to drift into
  reporting numbers the hook did not brake on. The verdict line goes further and
  calls `hook.decide` itself, so `status` cannot say `running` about a folder the
  hook is holding.

## 0.2.0 -- not released

Neither 0.1.0 nor this has been published, so there is no upgrade path here to
describe; the number moved so that a build can be told apart from the one before
it, which `niceclaude version` could not do while it never changed.

### Added

- `niceclaude version` (and `--version`) prints the installed version, read from
  the distribution's metadata. It reports what is *installed* rather than what a
  source file claims. Note what it cannot do: for a git install the number does
  not move between commits either, so `--help` and `status` remain the checks for
  whether the code is current.
- This file, and a comment on the version line in `pyproject.toml` asking for an
  entry here whenever that line moves. A tag that disagrees with the declared
  version is caught mechanically by `publish.yml`; an undescribed release is not.

### Changed

- The version is stored in exactly one place, `pyproject.toml`. The `__version__`
  literal in `__init__.py` is gone: nothing read it, and nothing kept it in
  agreement with the copy that `publish.yml` actually checks against the git tag.

## 0.1.0 -- not released

No tag exists and nothing has been published to any index. Two gates stand in
front of the first release, both recorded in `harness/open-questions.md` and
summarised in `RELEASING.md`: whether Claude Code invokes hooks synchronously on
Windows (the blocking *is* the brake), and one real paced run against the
default pace line rather than a forced one.

### The tool

- Pace Claude Code's background work against its own usage windows, against a
  pace line `allowed(f_t) = m0 + f_t * (100 - m0 - m1)`: if you are `f_t` of the
  way through a window's time, you should be at most `f_t` of the way through its
  budget. A `PreToolUse` hook blocks when a paced folder is ahead of the line.
- `niceclaude install` registers the hook by **merging** into Claude Code's
  `~/.claude/settings.json`, so no special launch flag is needed afterwards.
  Unrelated settings, and other people's hooks on the same events, survive; a
  file that cannot be parsed is refused rather than overwritten; running it twice
  updates in place instead of registering the hook twice.
- `niceclaude uninstall` removes exactly what `install` added and leaves policy
  and logs alone.
- `NICECLAUDE_OFF` set to any non-empty value exempts a single session. This is
  per-process, which is finer-grained than any settings file, and is what makes a
  global install safe.
- Per-folder policy: `on`, `off`, and a `global` master switch. A folder may
  declare its `--model`, override `--m0` / `--m1`, demand an extra
  `--fanout-reserve` of `SubagentStart`, and choose with `--enforce` which of the
  session, week and model windows it is paced against. A rule on the filesystem
  root acts as a catch-all.
- Folders with no matching rule are a genuine no-op: the hook answers "is this
  folder paced?" from `policy.json` alone, with no snapshot read, no subprocess
  and no network.
- `watch` polls usage in the background; `sample` and `refresh` take one reading;
  `check` runs misparse assertions over the log. A paced folder self-heals if the
  daemon is not running, by refreshing on demand at a cost of ~2s on that call.
- `status` explains the policy for a folder *and* whether anything is actually
  registered to enforce it, because "paced" and "plumbed" are separate facts and
  reporting the first while the second is false is this tool's worst failure
  mode. `list` shows every configured folder.
- `burn` characterises burn rate and duty cycle; `plot` graphs utilisation
  against the pace line, under the optional `plot` extra.
- Pure Python, and dependency-free on Linux and macOS. Windows declares `tzdata`,
  because it ships no tz database and the reset time would otherwise have to be
  inferred as machine-local -- an inference that can fail *open*, the one
  direction this tool must not fail in.
