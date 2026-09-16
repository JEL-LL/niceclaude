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
