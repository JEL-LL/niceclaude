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

Nothing yet.

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
- `version` prints the installed version, read from distribution metadata so it
  reports what is installed rather than what a source file claims.
- Pure Python, and dependency-free on Linux and macOS. Windows declares `tzdata`,
  because it ships no tz database and the reset time would otherwise have to be
  inferred as machine-local -- an inference that can fail *open*, the one
  direction this tool must not fail in.
