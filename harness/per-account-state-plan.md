# Per-account state — plan for issue #1

The plan for *"Separate state per account when `CLAUDE_CONFIG_DIR` is set"*.
It records what is wrong today, the options considered, the design chosen with
every decision stated, how the open questions were settled, and the phases to
build it in. Symbols are cited by name, not line number, so this survives
edits to the code it describes.

**Status: revised after seven reviews; READY.** The review log is at the end.

**Test baseline before any change:** 499 passed at 543dd4d, which includes
the subagent cache-TTL feature. Re-baseline before Phase 1 begins.

---

## 1. The problem

One machine can run more than one Claude account, by launching some sessions
with `CLAUDE_CONFIG_DIR=~/.claude-work claude`. niceclaude has one data
directory no matter which account a process belongs to, so every account reads
and writes the same `state.json`, `usage.jsonl`, `hook.log` and `daemon.pid`.

Each individual sample is correct. The hook calls `refresh_snapshot`, which
runs `niceclaude refresh`, which runs `claude -p /usage` in `sample_once`, and
the environment is inherited all the way down. The fault is what happens after
that: `publish_state` does not record which account a snapshot came from, and
`run` in the hook trusts any snapshot younger than `MAX_STALE` (180s), or
`NEAR_STALE` (15s) near the line. So:

- **A hook in account B paces on account A's numbers** whenever A's snapshot is
  fresh enough. With both accounts busy, the snapshot keeps changing hands, and
  each account is sometimes paced on the other's usage. That produces holds
  that should not happen, and releases that should not happen.
- **A `watch` daemon starves every other account of refreshes.** It samples the
  account of the shell it started in, and keeps the snapshot permanently fresh.
  So account B's hooks almost never refresh, and are paced entirely on A.
- **The histories mix.** `usage.jsonl` interleaves two accounts, so `burn`,
  `plot` and `check` compute rates across unrelated series, and `check` can
  report a "usage DECREASED" that is really a change of account. `hook.log`
  mixes too.

**This one fails in both directions,** which is what makes it worse than most
bugs here. §11 of `design-decisions.md` settles every ambiguity toward braking.
This one is not ambiguous from the hook's point of view. The hook sees a fresh
snapshot and acts on it confidently. Where A is well under its line, B can
spend straight past its own. That is the *looks paced, isn't* failure.

---

## 2. How paths resolve today

Everything is resolved at import in `_shared`, from the environment:

- `_data_dir()` returns `NICECLAUDE_DIR` verbatim when it is set. Otherwise it
  returns `%LOCALAPPDATA%\niceclaude` on Windows or `~/.local/share/niceclaude`
  on POSIX.
- `_config_dir()` returns `NICECLAUDE_CONFIG_DIR` when set, else
  `<NICECLAUDE_DIR>/config` when that is set (decision 16), else
  `%APPDATA%\niceclaude` or `~/.config/niceclaude`.
- `DATA_DIR` and `CONFIG_DIR` are module constants. `LOG_PATH`, `STATE_PATH`,
  `POLICY_PATH` and `HOOK_LOG_PATH` are joined onto `DATA_DIR`, and
  `SETTINGS_PATH` (the `--settings` fragment) onto `CONFIG_DIR`.
- `cli.PID_PATH` is `DATA_DIR/daemon.pid`. The TTL work (543dd4d) adds
  `CLAUDE_SETTINGS_MARKER_PATH` under `DATA_DIR` too. It is already a map keyed
  by Claude settings path, so one file serves every `CLAUDE_CONFIG_DIR` (§9).

**`CLAUDE_CONFIG_DIR` is read in exactly one place:** `cli.claude_settings_path()`.
It is resolved on every call, not at import, and it picks the Claude
`settings.json` that `cmd_install`, `cmd_uninstall` and `describe_installation`
operate on. The data dir never consults it.

**The hook's environment is Claude's environment.** `platform-findings.md` §6
records that the hook process inherits the wrapper's environment. Claude's hook
documentation says the same, with two exceptions: `OTEL_*` variables, and
anything `CLAUDE_CODE_SUBPROCESS_ENV_SCRUB=1` strips. So a session started with
`CLAUDE_CONFIG_DIR` exported normally has it set in every hook, and in every
`niceclaude refresh` and `claude -p /usage` those hooks spawn. A session
started with `claude --config-dir` might not; Phase 0 checks.

**What pins the current paths in the suite.** `tests/conftest.py` does
`os.environ.setdefault` for *both* `NICECLAUDE_DIR` and `CLAUDE_CONFIG_DIR`
before anything is imported. Every path-sensitive test then monkeypatches the
module constants (`POLICY_PATH`, `STATE_PATH`, `HOOK_LOG_PATH`, `LOG_PATH`,
`SETTINGS_PATH`) directly. `test_install_merge` pins `claude_settings_path()`
with and without `CLAUDE_CONFIG_DIR`, and `tests/smoke_installed.py` sets both
variables and hand-writes `state.json` at `NICECLAUDE_DIR/state.json`.

Three consequences follow:

- **The paths must stay module-level constants,** or those monkeypatches
  silently stop reaching the code.
- **`NICECLAUDE_DIR` must go on winning outright,** or the whole suite moves.
- **Every test process has a non-default account key,** because conftest always
  sets `CLAUDE_CONFIG_DIR`, and it uses `setdefault`, so a developer shell that
  already exports `CLAUDE_CONFIG_DIR` changes that key. Anything that compares
  keys has to be pinned in the suite. See Phase 2.

---

## 3. What the issue gets right, and what it gets wrong

The diagnosis is correct in every particular, and so is the workaround. Five
points need correcting before it can drive an implementation.

1. **It never says `install` is per account, and the workaround needs that
   too.** `cmd_install` writes into `claude_settings_path()`, which is
   `$CLAUDE_CONFIG_DIR/settings.json`. An account whose config dir never had
   `niceclaude install` run against it has no hook at all, and is not paced
   whatever the data dir says. This is true today and remains true after the
   fix. It needs surfacing (Phase 4), not only documenting.
2. **"And probably the config dir" — no.** `CONFIG_DIR` holds only the
   `--settings` fragment, and the fragment is byte-identical for every account:
   the hook command and its timeout. Keying it would multiply one file for
   nothing.
3. **Put the account directories under `accounts/`, not straight into the data
   dir.** The issue proposes `<data>/<slug>/`. A dedicated `accounts/` keeps
   account directories out of the legacy file set, and gives one place to list
   them.
4. **The stamp is not a nice-to-have.** Keying by path cannot catch the cases
   where two accounts still end up in *one* directory:
   - a `NICECLAUDE_DIR` shared across accounts;
   - a CRC collision between two slugs;
   - an unstamped snapshot, including one from a pre-upgrade daemon that keeps
     writing to the root.

   The stamp is what turns those into refresh churn rather than mis-pacing. It
   does nothing for processes that normalize the key differently. Those land in
   different directories and never meet, so the key has to follow Claude, not
   rely on the stamp (D4).
5. **On the hot path the stamp has to be the config-dir key, not an account
   id.** The hook has no cheap way to learn an account id. Nothing in `/usage`
   carries one (§6), and Claude's global config is tens of KB and growing. The
   account id belongs in the CLI, as a diagnostic.

---

## 4. Options

### A. Key the data dir by `CLAUDE_CONFIG_DIR`

This is the issue's proposal, laid out under `accounts/`. When
`CLAUDE_CONFIG_DIR` is set and is not the default, per-account files move to
`<root>/accounts/<slug>/`. `<root>` is today's data dir.

- **Changes:** `_shared` only, plus `cli.PID_PATH`, which follows `DATA_DIR`
  automatically.
- **Back-compat:** with `CLAUDE_CONFIG_DIR` unset or pointing at the default,
  every path is exactly as it is today.
- **Daemon:** one per account, started with that account's environment. Each
  has its own pidfile, so they do not collide, and `stop` stops the daemon for
  the current shell's account.
- **`on <folder>`:** depends on the policy decision (D1).
- **Risks:** it does nothing about a directory shared on purpose or by
  accident.

### B. One data dir, with the account key stamped into `state.json`

`publish_state` writes the key, and `run` treats a snapshot with a different
key as missing.

- **Correctness:** it closes the mis-pacing.
- **What it leaves broken:** `usage.jsonl` and `hook.log` still mix, so `burn`,
  `plot` and `check` all need filtering. There is still only one daemon.
- **What it does to latency:** with two accounts busy, each overwrites the
  other's snapshot. Nearly every paced tool call then pays a ~2s refresh, and
  both accounts' writers race for one file.

**Rejected as the fix.** It turns the bug into steady refresh churn, and keeps
the history mixed. It is kept as the guard in C.

### C. A and B together, plus log filtering — recommended

Key the directory as in A, and stamp and check the key as in B. Filter
`load_log` by the same key, so that a directory that is still shared (the D3
case) gives each account only its own history.

With the configuration right, the check never fires, and it costs one string
comparison. With it wrong, the failure becomes refresh churn plus a line in
`hook.log`, never mis-pacing. That is the same trade §12 makes: stale data may
justify braking, never allowing.

### D. One daemon for every account

`watch --all-accounts` samples each account it knows of, overriding
`CLAUDE_CONFIG_DIR` for each `claude -p /usage`, and publishes into that
account's directory.

**Not in this plan (Q4).** It is kept only as a future idea. Building it would
need the Phase 4 registry and a root-level pidfile, distinct from the
per-account ones.

The costs are known already:

- It samples serially, so one hung `claude` delays every other account. The
  timeout in `sample_once` is 120s.
- It can sample accounts nobody is using. Their credentials may have expired,
  and those surface as permanent poll failures.

It is a deployment convenience, not a correctness fix.

### E. `install` writes `NICECLAUDE_DIR` into the account's own settings

`install` would add `"env": {"NICECLAUDE_DIR": "<root>/accounts/<slug>"}` to
`$CLAUDE_CONFIG_DIR/settings.json`. Claude applies a settings `env` block to its
process environment, and hooks inherit it, so this is the workaround made
automatic, at zero hot-path cost.

**Rejected,** for three reasons:

- **It fixes only the hook.** `watch`, `status`, `burn` and `plot` run from a
  shell and never see a settings `env` block, so the user must still export the
  variable by hand for each of them. The hook would take its paths from Claude's
  settings while the CLI takes them from the environment: two sources of truth
  for one path, and the drift between them is the original bug again.
- **It forces per-account policy.** `NICECLAUDE_DIR` relocates `policy.json` as
  well, which is against D1.
- **It is one more key written into a file niceclaude does not own.** `uninstall`
  would then have to remove it (decision 8's round-trip guarantee).

### Also considered: key by account UUID

This would name the directory after `oauthAccount.accountUuid`, so two config
dirs logged into the same account would share one snapshot, which is correct.
**Rejected for now.** The hook would need the UUID on every call. It could read
Claude's global config (tens of KB, parsed per call) or a cache the CLI keeps,
and until that cache exists the account runs split-brained. Revisit only if
several config dirs on one account turns out to be a real pattern.

---

## 5. Recommended design (option C)

### Paths

`_shared` gains a shared root and a per-account directory:

```
ROOT_DIR     = today's _data_dir()     # NICECLAUDE_DIR verbatim, or the platform default
ACCOUNT_KEY  = "" for the default account, else the normalized config dir (D4, D10)

DATA_DIR     = ROOT_DIR                  when NICECLAUDE_DIR is set (D3)
             = ROOT_DIR                  when ACCOUNT_KEY == ""
             = ROOT_DIR/accounts/<slug>  otherwise

POLICY_PATH                 = ROOT_DIR/policy.json                   shared (D1)
CLAUDE_SETTINGS_MARKER_PATH = ROOT_DIR/claude_settings_marker.json   shared (§9)
REGISTRY_PATH               = ROOT_DIR/accounts.json                 shared (Phase 4)
STATE_PATH, LOG_PATH, HOOK_LOG_PATH                                  under DATA_DIR
cli.PID_PATH                                    under DATA_DIR (follows automatically)
CONFIG_DIR, SETTINGS_PATH                       unchanged (§3, point 2)

account_paths(config_dir, env) -> {config_key, slug, data_dir, state_path,
                                   log_path, hook_log_path, pid_path}
```

`account_paths` returns the slug itself, so the slug is computed in one place.
It is `""` for the default account.

**`niceclaude paths` prints exactly ten JSON keys, and no others:** the seven
that `account_paths` returns, plus `root_dir`, `policy_path` and
`registry_path`. `smoke_installed.py` and the deploy scripts read them by these
names, so they are a contract.

**`niceclaude paths <key>` prints a single value.** It takes an optional
positional key, and prints just that value on one line: for example,
`niceclaude paths pid_path`. That is the form the POSIX-sh entrypoint uses,
because busybox has no `jq` and `python3` is not guaranteed on `PATH`. An
unknown key exits nonzero. The argument carries help text, which `test_help`
requires.

**`account_paths` takes the home directory from `env`, not from the process.**
`os.path.expanduser` reads `os.environ`, so a function that ignored `env` would
quietly test the developer's real home.

- When `env` is supplied, the default dir is
  `os.path.join(env.get('USERPROFILE') or env.get('HOME') or HOME, '.claude')`.
- A leading `~` in `config_dir` is expanded against that same home.
- **The root is resolved from `env` as well:** `NICECLAUDE_DIR` if set, else
  `LOCALAPPDATA` on `nt`, else the `env` home. That is exactly the logic of
  `_data_dir()` today, but it reads `env`. `account_paths` never reads the
  import-time `ROOT_DIR`.
- **`slug` is computed from any non-empty key, even when `NICECLAUDE_DIR` is
  set.** Only `data_dir` ignores the slug in that case (D3).

`account_paths` computes the account-scoped paths for any config dir. The
import-time constants are simply its result for the environment's config dir.
The hook uses it again when the payload supplies the config dir (D10).

`ACCOUNT_KEY` is computed even when `NICECLAUDE_DIR` is set, because the stamp
(D6) and the log filter (D11) need it exactly in that case.

`DATA_DIR` keeps its name and now means the account's directory. Every
remaining use of it (`cmd_install`, `cmd_watch`, `PID_PATH`) is already about
account-scoped state, so it needs no edit. The exception is the
`cmd_uninstall` message, which Phase 1 extends to name `ROOT_DIR` as well.

Three files move to the new `ROOT_DIR`:

- `POLICY_PATH`, which is shared by D1.
- `CLAUDE_SETTINGS_MARKER_PATH`, which stays where it is today (§9).
- `REGISTRY_PATH`, which is new.

The resolvers take an `env` mapping, defaulting to `os.environ`, so tests can
drive them directly. The constants are still computed once at import, so the
existing monkeypatches keep working.

### Decisions

**D1. `policy.json` is shared across accounts.** Policy is about folders
(decision 7). Usage is about accounts. Decision 9 already splits those two, and
this plan keeps the split.

A per-account policy would reintroduce *looks paced, isn't*: run `on` in a
shell without the work account's environment, and that account's hooks never
see the rule. A shared policy also means the same `m0`/`m1` mean the same thing
for every account, because each is measured against its own snapshot.

The cost is that one folder cannot be paced differently per account. That was
Q1, and it is settled: shared. `install --force` resets the shared file, and
must name the accounts in the registry that this affects.

**D2. Unset, empty, or default `CLAUDE_CONFIG_DIR` all map to the legacy
directory.**

- **Empty** matches the `or` fallback `claude_settings_path()` already uses.
- **Default** means the value's realpath-normalized form equals that of
  `~/.claude`.

Without the default rule, anyone who exports `CLAUDE_CONFIG_DIR=~/.claude` in
their profile loses their history on upgrade. This was Q2, and it is settled:
map to the legacy dir by realpath comparison. Q2 records the macOS caveat.

**The comparison costs 0.12–0.3ms per `norm_path` on Windows.** Review 3
measured the lower figure for `norm_path`, and the plan's own measurement of
bare `realpath` gave the higher. Two things keep it cheap:

- **Compare `normcase(normpath())` of both sides first.**
  - Equal means default, and no `realpath` runs.
  - Unequal settles nothing, because a symlinked spelling may still be the
    default. Both sides then go through `norm_path`. The config dir's result
    is needed for the key anyway, and the default's comes from the cache.
- **Cache the normalized default per home, not per process.** The cache is a
  one-entry map from a home string to `norm_path(<home>/.claude)`. The home
  string is the one the default was built from: `USERPROFILE` or `HOME` from
  `env`, else the module's `HOME`.
  - A call whose home differs recomputes the default and replaces the entry.
  - In the hook the home never changes within a process, so this is still at
    most one `norm_path` per process.
  - In the suite, a test that redirects the home gets a default built from
    `tmp_path`. A per-process cache would instead be filled at import, from
    the developer's real `~/.claude`, because conftest sets a non-default
    `CLAUDE_CONFIG_DIR` before anything is imported.

**D3. `NICECLAUDE_DIR` wins outright, and its path is never slugged.** An
explicit data dir means exactly that directory. `slug` is still computed from a
non-empty key (§5, *Paths*); only `data_dir` ignores it. This keeps the issue's workaround working
unchanged, keeps `conftest.py` and `smoke_installed.py` unchanged, and keeps
decision 16's one-bind-mount property. Two accounts sharing one `NICECLAUDE_DIR`
are separated by the stamp (D6) and the log filter (D11), not by the path.

**D4. Normalization is `norm_path`'s steps, applied after `~` is expanded
against the `env` home.** `norm_path` itself calls `expanduser`, which reads
`os.environ`. So a leading `~` is first replaced with the `env` home (§5,
*Paths*), which makes `expanduser` a no-op. The remaining steps are exactly
`norm_path`'s: `realpath`, `normpath`, `normcase`.

- Trailing separators are stripped.
- Symlinks resolve on both sides of the default comparison.
- On Windows, `C:/Users/x/.claude-work` and `C:\Users\x\.claude-work` fold to
  one key through `normpath` and `normcase`.
- `normcase` folds case on Windows only. On macOS a differently cased spelling
  gets a different slug, which costs a split history, not mis-pacing.
- A relative value is resolved against the process's cwd, which differs per
  session. That is a misconfiguration, and `status` warns about it.

**The key must follow Claude, because the stamp cannot catch a disagreement
between niceclaude and Claude.** If niceclaude expands a literal `~` and Claude
does not, the two are talking about different directories, and every
niceclaude process agrees with the others while all of them disagree with
Claude. Phase 0 checks what Claude does with a literal `~`, and the key follows
that.

There is no MSYS drive translation. Measured here, Git Bash hands a native
process `C:/Users/...` for an exported `/c/Users/...`, which `normpath` already
folds. A translation would also be wrong on its own terms: `os.path.realpath`
resolves `/c/Users/Joshu` against the *current* drive, giving `F:\c\Users\Joshu`,
so any translation would key a directory Claude is not using.

**D5. Slug = readable basename + CRC32 of the full key.** For example,
`~/.claude-work` becomes `accounts/claude-work-1a2b3c4d`.

- The basename is built in five steps:
  1. lowercase it;
  2. strip leading dots;
  3. replace every character outside `[a-z0-9._-]` with `-`;
  4. collapse runs of `-` into one;
  5. cap it at 32 characters.

  For example, `My Claude Work` becomes `my-claude-work-<crc>`.
- **Never `hashlib`.** A cold `import hashlib` measured roughly 9–37ms
  cumulative across runs on CPython 3.13.7 here, against a 16ms hook. `zlib`
  was under 0.2ms, and `realpath` 0.3ms.
- Import `zlib` lazily, and only for a non-empty key. The default account's
  hot path gains nothing. The Phase 1 scrubbed-subprocess test is unaffected:
  it uses the default account.
- A CRC32 collision among a handful of accounts is not a practical concern. If
  one happens, the stamp compares the full key and turns it into churn, not
  mis-pacing.

**D6. `state.json` and every `usage.jsonl` record carry `config_key`.** It is
the `ACCOUNT_KEY` of the process that sampled.

A new `hook.load_state(path, key)` reads `path` and applies the check. It is
given both arguments explicitly, and reads no module globals, so each caller's
own test pins keep working:

- `run` calls it with the hook's `STATE_PATH` and `ACCOUNT_KEY`, looked up at
  call time, at both places it loads the state (before the refresh and after
  it).
- `cmd_status` calls it with `cli.STATE_PATH` and `cli.ACCOUNT_KEY`. That
  matters because `test_band_cli`, `test_wait_report` and `test_exempt` pin
  only `cli.STATE_PATH`.

**Contract: `load_state(path, key) -> (state, foreign)`.**

- An accepted snapshot returns `(state, None)`.
- **A missing or unparseable `state.json` returns `({}, None)`.** A parsed
  value that is not a dict counts as unparseable. It is absent, not foreign.
  Otherwise:
  - `run` would log `foreign snapshot` on every fresh account's first call;
  - `status` would print "written by another account" in place of today's
    `no snapshot yet -- is \`niceclaude watch\` running?`.
- A parsed snapshot that is rejected returns `({}, foreign)`. `foreign` is the
  stamp it found, or `"<unstamped>"` when the snapshot parsed but carried no
  `config_key`. The `{}` also makes its age `None`.
- `load_state` writes no log itself, so `status` never writes to `hook.log`.

**`cmd_status` returns 0 early whenever the state is `{}`,** before the age
and bucket table. It has to stop there, because that table indexes
`st["ts_epoch"]`, which raises `KeyError` on `{}`. There are three cases:

- `foreign` is set: it prints
  `snapshot: written by another account (<foreign>) -- ignored`.
- `foreign` is `None` and the file is present: it prints
  `snapshot: unreadable -- delete it or restart niceclaude watch`.
- The file is absent: it prints today's `no snapshot yet` line.

The `{}` is load-bearing. Treating the snapshot only as stale would still hand
its buckets to `decide(degraded=True)`, which brakes *"with full confidence"* on
anything over the line. Another account's usage is not a lower bound on ours,
so §12's monotonicity argument does not apply to it. With `{}`:

- the stale-age path forces a refresh;
- if that refresh fails, `decide` finds no enforceable bucket and returns
  `blind: True`, `hold: "hard"`, reason `"no usable buckets in snapshot"`,
  which is the honest answer.

`run` logs the mismatch once per invocation, as `foreign snapshot (<foreign>)`
in `hook.log`. Optionally, `decide` could also carry that reason in place of the
generic one. The comparison is a string compare, which adds no import and no
syscall.

**D7. A missing stamp matches the default key only.**

- Snapshots written by old code stay valid for single-account users.
- A non-default hook treats an unstamped snapshot as foreign, and pays one
  refresh to replace it.

The one case this trusts wrongly is a pre-upgrade daemon that was started
under a work `CLAUDE_CONFIG_DIR` and still writes unstamped work-account
snapshots into the root. D9 is the answer to it, and Phase 2's `status`
warning is how it is spotted.

**D8. The account identifier is diagnostic and read only by the CLI.** It is
`oauthAccount.accountUuid` together with `oauthAccount.organizationUuid`, read
from Claude's global config (§6).

- It is stamped into `usage.jsonl` and `state.json` as `account`.
- `check` reports a UUID that changes within one account directory's log,
  which is how a `/login` to a different account shows up.
- `check` also reports two account directories that share a UUID. It reads
  `account` from `ROOT_DIR/state.json` and from each
  `ROOT_DIR/accounts/*/state.json`. Those are small files; it never reads
  another account's log, which D11 would filter anyway.
- The hook never reads it.
- Email, display name, tokens and `.credentials.json` are never read, stored
  or printed.

**D9. Upgrade and downgrade: stop every daemon first; nothing is migrated
automatically.**

- **Upgrade.** Stop every daemon before upgrading. A default-account daemon, and
  any daemon still running under a hand-set `NICECLAUDE_DIR`, keeps its pidfile
  where it was, so the new `niceclaude stop` finds it from the matching shell.
  A pre-upgrade daemon started under a work `CLAUDE_CONFIG_DIR` wrote its
  pidfile to the **root**. It is stopped by `niceclaude stop` run from a shell
  with `CLAUDE_CONFIG_DIR` *unset*.

  Left running, that daemon keeps publishing unstamped work-account snapshots
  into the root. The default account's hooks trust them under D7, which is the
  original bug, silently. Meanwhile a new `watch` in the work environment
  starts a second daemon for the same account. The CHANGELOG says this, in
  those words.
- **The `status` upgrade warnings.** These apply only when
  `DATA_DIR != ROOT_DIR`, and there are two:
  - (a) `ROOT_DIR/state.json` exists, is unstamped, and its `ts_epoch` is
    younger than `MAX_STALE`.
  - (b) `pid_alive` is true for the pid in `ROOT_DIR/daemon.pid`, **and**
    `ROOT_DIR/state.json` exists with no `config_key` key at all.

    A stamp of `""` does not trigger it: that is the default account's own new
    daemon, which legitimately owns the root pidfile in ordinary two-account
    use. Checking only "the pid is alive and this account has no daemon" would
    tell a work account to kill it.

  Both warnings build their paths at call time, as
  `os.path.join(cli.ROOT_DIR, "state.json")` and
  `os.path.join(cli.ROOT_DIR, "daemon.pid")`, so tests can pin them.

  Both print: *"a daemon holding the legacy pidfile has not published a stamped
  snapshot; if it predates this version, stop it from a shell with
  CLAUDE_CONFIG_DIR unset and restart `watch`"*.

  The wording is conditional on purpose. (b) can also fire for the default
  account's new daemon, when it holds the root pidfile but has never
  published: logged out, for example. The root snapshot is then still the old
  unstamped one, and nothing in `status` can tell that daemon from an old one.
- **History.** Someone already running a single non-default `CLAUDE_CONFIG_DIR`
  without `NICECLAUDE_DIR` will find their history left in the legacy
  directory. Moving it takes two `mv`s, and `status` prints them. Nothing moves
  files automatically, least of all from the hook's hot path. Until the history
  is moved, the account's own directory has no history, so its `burn` and
  `plot` start from empty.

  `status` prints the hint when all of these hold:
  - `ACCOUNT_KEY != ""`;
  - `NICECLAUDE_DIR` is unset;
  - `ROOT_DIR/usage.jsonl` or `ROOT_DIR/hook.log` exists;
  - `DATA_DIR/usage.jsonl` does not exist.

  It prints *"only if that history was recorded by this account alone; a mixed
  log cannot be split"*, then one fully resolved `mv` line per file that
  exists: `usage.jsonl` and `hook.log`. It never names `state.json`,
  `policy.json`, the TTL marker, or the registry.
- **Downgrade.** It needs no migration. Old code ignores `config_key` and
  `account`, and reads the legacy paths, which are untouched. Only history
  under `accounts/` is invisible to it.

**D10. The config dir may come from the hook payload as well as the
environment.** This depends on Phase 0, where F3 of the review (log below)
applies.

`claude --config-dir` and `CLAUDE_CODE_SUBPROCESS_ENV_SCRUB=1` may each leave a
hook without `CLAUDE_CONFIG_DIR`. If they do, the hook derives the config dir
from `transcript_path`.

**The config dir is the parent of the nearest ancestor named `projects`.** It
is not a fixed number of `dirname`s up. A transcript has two shapes, and both
exist on this machine:

- `<config>/projects/<slug>/<session>.jsonl` in the main agent;
- `<config>/projects/<slug>/<session>/subagents/agent-<id>.jsonl` in a
  subagent.

A path with no `projects` component yields no candidate.

**The walk is string work, but the default-account test after it is not.**
Finding `projects` needs no I/O. Deciding whether the result is the default
account needs `norm_path` on both sides, at 0.12–0.3ms each, with the
short-circuit and cache described under D2.

**Precedence.** The environment wins when `CLAUDE_CONFIG_DIR` is set and
non-empty. The payload fills in only when it is missing or empty.

**Disagreement.** If the two still disagree after `normcase(normpath())` of
both, without `realpath`, the disagreement is logged and never acted on. A
symlinked spelling can then log a false disagreement. That costs one line in
`hook.log` and nothing else, and a `realpath` on every call is not worth
paying to eliminate it.

A payload without
`transcript_path` falls through to the environment; `smoke_installed.py`'s
`hook_blocks` sends only `cwd` and `hook_event_name`, which is that case.

**Mechanism.** The design keeps §2's module-constant rule.

- **The derivation happens after the `os.path.exists(POLICY_PATH)` gate, and
  before `run` is called,** so a session with no policy file pays nothing.

- **`main()` derives the config dir from the payload only when the environment
  has no `CLAUDE_CONFIG_DIR`.** It then calls
  `account_paths(config_dir, os.environ)`.
- **If the result's `config_key` is `""`, `main()` rebinds nothing,** and
  leaves `CONFIG_DIR_OVERRIDE` at `None`. The default account keeps today's
  environment exactly.

  That rule is load-bearing. For a default-account user every
  `transcript_path` sits under `~/.claude/projects`. Rebinding on it would run
  every default refresh as `claude -p /usage` with `CLAUDE_CONFIG_DIR`
  explicitly set, which today runs with it unset. Q2's caveat says a set
  variable may not mean the same login, and Phase 0 B has not shown that it
  does. If it does not, every default refresh fails and pacing freezes.
- **Only a non-empty key rebinds.** `main()` then rebinds `ACCOUNT_KEY`,
  `STATE_PATH`, `HOOK_LOG_PATH` and `CONFIG_DIR_OVERRIDE` before calling `run`.
- When the environment is set, `main()` touches nothing. A rule of "rebind
  whenever it differs from the import-time value" would clobber the suite's
  monkeypatched paths, because the Phase 2 fixture pins `ACCOUNT_KEY=""` while
  conftest sets `CLAUDE_CONFIG_DIR`.
- `run` keeps reading the globals, which is what the tests' monkeypatches set.
- `POLICY_PATH` is never rebound, because it is shared.
- **`refresh_snapshot` keeps its zero-argument signature.** Seven existing
  tests replace it with a zero-argument callable: four in `test_band`, one in
  `test_max_delay`, and two in `test_hook_ordering`. `run` calls it as
  `refresh_snapshot()`.
- It reads a new module global instead, `hook.CONFIG_DIR_OVERRIDE`, which
  defaults to `None`. `main()` rebinds it alongside `ACCOUNT_KEY`,
  `STATE_PATH` and `HOOK_LOG_PATH`.
- When the override is set, `refresh_snapshot` spawns the child with
  `env={**os.environ, "CLAUDE_CONFIG_DIR": CONFIG_DIR_OVERRIDE}`, so the refresh
  samples and publishes into the same account. When it is `None`, the child
  inherits the environment exactly as today.

**D11. `load_log` drops only records stamped by another account.**

- A record whose `config_key` matches is kept.
- A record stamped with a different key is skipped and counted.
- An unstamped record is kept under **every** key.

The last rule is deliberately looser than D7. A snapshot is transient, and
wrongly trusting one mis-paces; history is permanent, and wrongly hiding it
cannot be undone, since it can never be re-attributed. An unstamped record
predates the stamp, and once D9 has stopped the old daemons nothing writes one
again.

Keying unstamped records to the default account alone would hide all of a
migrated work account's history, and all of a `NICECLAUDE_DIR` user's history,
contradicting D9 and Q5. The cost is that a directory two accounts shared
before the upgrade stays mixed for its pre-upgrade span. That was already true,
and it is why D9 says a mixed log cannot be split.

`load_log` keeps its zero-argument signature and its list return, because
`test_plot` pins it as `lambda: []`, and it filters internally. When it skips
anything it prints `skipped N records from other accounts` to stderr, the same
way it already reports `corrupt JSON at log line n`. `check`, `burn` and `plot`
need no change.

Without this, option C plus D3 still mixes histories under a shared
`NICECLAUDE_DIR`. `cmd_check` would flag false decreases, and `burn` and
`plot.collect` would difference across accounts.

### Behaviour after the change

- **`niceclaude on <folder>` / `off` / `global` / `list`** write and read the
  shared policy, so they work from any shell. Running `on` again per account
  is no longer needed.
- **`niceclaude install`** is still per account (§3, point 1). It also records
  the account in a registry, `REGISTRY_PATH`, mapping each config dir to its
  slug and hook state (Phase 4). The registry is written with `write_atomic`,
  and the last writer wins.

  The registry key is computed from `os.environ` at call time, exactly as
  `claude_settings_path()` is, and never from the import-time `ACCOUNT_KEY`.
  Otherwise Phase 2's autouse fixture would register every install as the
  default account, and `test_subagent_cache_ttl` changes `CLAUDE_CONFIG_DIR`
  between two installs in one process.
- **The registry entry** is
  `{"<ACCOUNT_KEY>": {"config_dir", "slug", "hook": bool, "ts"}}`. It records
  what `install` and `uninstall` did. It cannot mean "never installed", for two
  reasons. A hook can also come from project settings or the `--settings`
  fragment. And a slug cannot be mapped back to a config dir, so an entry is
  the only place `config_dir` is known.
- **`niceclaude uninstall`** keeps the registry entry but sets `"hook": false`.
- **`niceclaude watch` / `stop`** act on the current environment's account.
  Two daemons, one per account, coexist.
- **`status` / `burn` / `plot` / `check`** read the current environment's
  account, and `status` prints an `account:` line naming it.
- **`status` also lists every account it knows of:** the union of the registry
  entries and the `accounts/*` directories.
  - For a registered account, it evaluates the hook state live against
    `<config_dir>/settings.json`. A stored `hook: false` is shown as
    `(uninstalled on purpose)`.
  - An `accounts/<slug>` directory with no registry entry is shown as
    `no install recorded for <slug>`.
- **The hook** reads the shared policy, reads its own account's state and logs
  there, and refreshes into its own account. The `os.path.exists(POLICY_PATH)`
  early return in `main` still works, because policy lives at the root.

### Windows and Linux concerns

- **Slash direction on Windows** is folded by `normpath` (D4). There is no MSYS
  translation.
- **Path length.** `%LOCALAPPDATA%\niceclaude\accounts\<41-char slug>\state.json.tmp.<pid>`
  stays far below `MAX_PATH`.
- **`subst` and UNC.** `subst` drives resolve through `realpath`, and a UNC
  spelling does not (decision 7's closing note). A config dir reached both ways
  gets two directories, which splits history; the stamp does not see this.
- **The systemd unit.** Replace the hardcoded
  `ExecStopPost=... rm -f %h/.local/share/niceclaude/daemon.pid` by dropping
  `ExecStopPost` altogether. Its rationale is stale: decision 14 made SIGTERM
  unwind, and `cmd_watch`'s `finally` removes the pidfile. A templated
  `niceclaude@.service` cannot put `/` in an instance name, so it uses
  `Environment=CLAUDE_CONFIG_DIR=%h/.claude-%i`, or `systemd-escape --path`
  with `%I`.
- **`deploy/docker-entrypoint.sh`** clears `$NICECLAUDE_DATA/daemon.pid`. Under
  a non-default key without `NICECLAUDE_DIR`, that misses the pidfile silently,
  so the entrypoint asks the CLI for its paths (Phase 1's `paths` command).

---

## 6. What can identify an account

Sources checked against real data on this machine. Only key names and counts
were printed, never values.

- **`claude -p /usage` output: nothing.** A current sample has 16 lines:

  - the subscription preamble;
  - three bucket lines;
  - the advisory block.

  None of them names an account or organization, and none contains an `@`. The
  preamble distinguishes a subscription from an API key, and nothing more.
- **The config-dir key.** It is free in the hook, from the environment or from
  `transcript_path` (D10). It identifies a *config dir*, not an account, and
  that is the right unit for the hot path: it is what decides which
  credentials `claude` uses.
- **Claude's global config, `oauthAccount`.** It is `~/.claude.json` when
  `CLAUDE_CONFIG_DIR` is unset. Phase 0 confirms where it lives when the
  variable is set. It holds `accountUuid` and `organizationUuid` (both
  36-character UUIDs), next to fields that must not be touched: `emailAddress`,
  `displayName`, `fullName`, and others. **Use the pair,** not `accountUuid`
  alone. Limits attach to an organization seat, so one person in a personal org
  and a team org is two budgets. The top-level `userID` is not an account id,
  so do not use it.
- **Not used, ever:** `.credentials.json`, the OS keychain, tokens, and email.

Two traps in reading that file:

- **It is ~80 KB here and grows with project history.** That is fine once per
  poll, and wrong once per tool call. This is D8's reason for keeping it out of
  the hook.
- **Its `projects` map has keys that differ only by drive-letter case.** Parsed
  with `object_pairs_hook`, the file has no exactly duplicated keys, but it has
  six pairs of `projects` keys that differ only in the case of the drive letter.
  Python keeps both. PowerShell's `ConvertFrom-Json` treats keys
  case-insensitively and refuses the file, so any tooling that inspects it must
  not be PowerShell.

---

## 7. Questions for the user — settled

All five are settled. The user was away, so the orchestrator settled each
one, and two independent reviewers agreed on every decision. The question and
the reviewers' views are kept, so the reasoning can be revisited. "Reviewer 1"
and "Reviewer 2" are review rounds 1 and 2 in the log below.

> **Q1 — Is a shared `policy.json` acceptable?** The recommendation (D1) is
> shared, so a folder is paced the same way under every account. If some folder
> genuinely needs different rules per account, the later extension is an
> optional `accounts/<slug>/policy.json` overlaid on the shared file. Not
> proposed now.
>
> *Reviewer 1:* yes, shared. `install --force` should say which accounts it
> affects.
>
> *Reviewer 2:* shared. The TTL feature has already made the folder-level TTL
> independent of the account.
>
> **Decision:** `policy.json` is shared (D1).

> **Q2 — Should `CLAUDE_CONFIG_DIR=~/.claude` map to the legacy directory?**
> Recommended yes (D2). The caveat: with the variable *set*, Claude may read
> `$CLAUDE_CONFIG_DIR/.claude.json` rather than `~/.claude.json`, and on macOS
> it may key its keychain entry on the directory. So "same directory" might not
> mean "same login". Phase 3 can detect this by comparing the UUIDs from both
> locations, but it cannot prevent it.
>
> *Reviewer 1:* yes, by comparing realpaths. Phase 0 confirms that
> `.claude.json` moves under `CLAUDE_CONFIG_DIR`.
>
> *Reviewer 2:* yes, by comparing realpaths. Phase 0 checks that
> `CLAUDE_CONFIG_DIR=~/.claude` still reads `~/.claude.json`.
>
> **Decision:** a `CLAUDE_CONFIG_DIR` that realpaths to the default `~/.claude`
> maps to the legacy directory (D2).

> **Q3 — Is manual migration acceptable (D9)?** The alternative is a one-shot
> `niceclaude migrate` for single-account history. It would never be offered
> for a directory two accounts have shared, because those records cannot be
> attributed.
>
> *Reviewer 1:* manual `mv` only, and no migrate command, because mixed logs
> cannot be attributed.
>
> *Reviewer 2:* manual only, but `status` should print the exact `mv`, with
> both resolved paths. Phase 4 now does this.
>
> **Decision:** migration is manual only, with no migrate command. `status`
> prints the exact moves (D9).

> **Q4 — Is one daemon for every account (the former Phase 5) wanted at all,**
> or are per-account daemons with templated service files enough?
>
> *Reviewer 1:* not now.
>
> *Reviewer 2:* not now.
>
> **Decision:** dropped from this plan, and kept only as a future idea (§4 D).

> **Q5 — Should workaround users' `NICECLAUDE_DIR` be left alone?** D3 keeps
> them exactly as they are, with per-account policy and all. After this lands
> they could drop the workaround and rejoin the shared policy, but only by
> hand.
>
> *Reviewer 1:* yes, leave them alone. Document the rejoin path, and make sure
> the D11 filter lands.
>
> *Reviewer 2:* leave them alone. The rejoin documentation gives two steps:
> drop `NICECLAUDE_DIR`, then re-run `on` for each rule, or copy their
> `policy.json` to the root.
>
> **Decision:** `NICECLAUDE_DIR` users are left alone (D3), and the rejoin path
> is documented (Phase 4).

---

## 8. Phases

Each phase lists the existing tests it must touch or keep green. "Keep green"
means they must pass unedited. Where a phase has to edit an existing test, it
says so.

### Phase 0 — verify the assumptions, with no code

**The trap to avoid.** A scratch `CLAUDE_CONFIG_DIR` has no credentials, so no
session starts in it and no hook ever fires. Phase 0 therefore has two parts:
one that can run now, on the real account, and one that needs the user.
**Never copy credential files anywhere,** for either part.

**Procedure A — runnable now, on the real account, with `CLAUDE_CONFIG_DIR`
unset.**

1. Write a probe hook into `<scratch>/probe.json`, registered for `PreToolUse`
   and `SubagentStart`. It appends one JSON line per event to
   `<scratch>/probe.log`, containing:
   - `CLAUDE_CONFIG_DIR` from its environment, or `null`;
   - `hook_event_name`;
   - `agent_id`, if present;
   - `transcript_path`.

   It records nothing else, and never the payload's tool input.
2. Run `claude --settings <scratch>/probe.json -p "<prompt>"`, with a prompt
   that runs one Bash command and then one Task subagent that runs one Bash
   command.
3. Read `probe.log`. It answers three things:
   - whether Claude injects `CLAUDE_CONFIG_DIR` into a hook when it was unset;
   - the main-agent `transcript_path` shape;
   - which `transcript_path` a subagent's `PreToolUse`, and its
     `SubagentStart`, receive.
4. Record for `platform-findings.md` the Git Bash observation already measured:
   an exported `/c/...` reaches a native process as `C:/...`, and
   `realpath('/c/...')` resolves against the current drive.

**Procedure B — needs the user.** Every check with the variable *set* needs a
logged-in config dir other than the default. The user either runs
`claude /login` in a scratch config dir, or uses a second account they already
have. Then, with the same probe:

- `CLAUDE_CONFIG_DIR=<dir>`: the hook sees it. Also check where
  `.claude.json` is written.
- `CLAUDE_CONFIG_DIR=<dir>` with `CLAUDE_CODE_SUBPROCESS_ENV_SCRUB=1`: whether
  the hook still sees it.
- `claude --config-dir <dir>`, but only if `claude --help` lists the flag. It
  does not in 2.1.282.
- A literal, unexpanded `~` in `CLAUDE_CONFIG_DIR` (D4).
- `CLAUDE_CONFIG_DIR=~/.claude`: whether it still reads `~/.claude.json` (Q2).

**Until Procedure B has run, the implementation assumes:**

- the hook inherits `CLAUDE_CONFIG_DIR` unchanged, so D10's payload fallback
  stays unbuilt;
- a literal `~` is expanded, matching `norm_path`;
- `.claude.json` lives at `$CLAUDE_CONFIG_DIR/.claude.json` when the variable
  is set, and at `~/.claude.json` otherwise;
- `CLAUDE_CONFIG_DIR=~/.claude` is the default login (D2).

Each assumption is marked in the code where it is relied on.

- **Scope:** Procedures A and B. Then confirm that D10's precedence (the
  environment wins; the payload only fills in) survives what the checks show.
- **Files:** findings go in `platform-findings.md`. Probe files stay in
  scratch. Never print credentials.
- **Tests:** none. Existing tests: none.

### Phase 1 — account-keyed paths

- **Scope:**
  - Add `ROOT_DIR`, `ACCOUNT_KEY`, `REGISTRY_PATH`, `account_paths()` and the
    slug function to `_shared`.
  - `DATA_DIR` becomes the account directory. `POLICY_PATH` and
    `CLAUDE_SETTINGS_MARKER_PATH` are anchored at `ROOT_DIR`.
  - Resolvers take an `env` mapping.
  - Import `zlib` lazily, as D5 says.
  - Add a `niceclaude paths` command that prints the fixed JSON keys listed in
    §5, *Paths*. It is used by deploy scripts and by `smoke_installed.py` in
    Phase 2.
- **Files:**
  - `src/niceclaude/_shared.py`.
  - `src/niceclaude/cli.py`: the import list; the `paths` command, with its
    `COMMAND_HELP` entry (see below); and the `cmd_uninstall` message, which
    should name both directories.
  - A new `tests/test_account_paths.py`.

  The `COMMAND_HELP` entry for `paths` must meet `test_help`: a description of
  at least three lines, every line within 79 characters, and help on every
  argument. That includes the optional positional `key` of the single-value
  form (§5, *Paths*).
- **New tests:**
  - Unset, empty, and default `CLAUDE_CONFIG_DIR` each give today's paths
    exactly, compared against a copy of the old resolver.
  - The same directory spelled with a trailing slash, with `~`, and (on
    Windows) in a different case gives one slug.
  - `C:/Users/x/.claude-work` and `C:\Users\x\.claude-work` give one key on
    `nt`.
  - On POSIX, a symlink to `~/.claude` gives the legacy directory. On Windows,
    skip unless symlinks are permitted.
  - Two different directories give two slugs, both under `accounts/`, both
    made of safe characters, and short.
  - A mixed-case basename with a space, such as `.../My Claude  Work`, becomes
    `my-claude-work-<crc8>`, pinning each of D5's five steps.
  - `account_paths(..., env)` takes its home from `env`: with `USERPROFILE`
    and `HOME` pointed at `tmp_path`, both the default comparison and the `~`
    expansion use `tmp_path`, and never the real home.
  - With `NICECLAUDE_DIR` set, `DATA_DIR` ignores `CLAUDE_CONFIG_DIR`, but
    `ACCOUNT_KEY` still reflects it.
  - `POLICY_PATH`, `CLAUDE_SETTINGS_MARKER_PATH` and `REGISTRY_PATH` are
    identical across every case.
  - Pidfiles (new coverage; nothing in the suite tests `watch` or `stop`
    today):
    - `account_paths(A)['data_dir'] != account_paths(B)['data_dir']`, and the
      same for `pid_path`.
    - The default key gives `ROOT_DIR/daemon.pid`.
    - `cli.PID_PATH == os.path.join(cli.DATA_DIR, "daemon.pid")`.
  - `account_paths()` agrees with the import-time constants for the
    environment's own config dir.
  - A subprocess import of `niceclaude.hook`, with `NICECLAUDE_DIR` and
    `CLAUDE_CONFIG_DIR` scrubbed from the child environment, loads neither
    `hashlib` nor `zlib`.
  - An end-to-end subprocess test.
    - **Environment:** `HOME`, `USERPROFILE` and `LOCALAPPDATA` redirected to
      `tmp_path`; `NICECLAUDE_DIR` unset; `CLAUDE_CONFIG_DIR` set.
    - **The child is not a `claude` stub on `PATH`,** because that fails on
      Windows. `sample_once` calls `subprocess.run(["claude", ...])` without a
      shell, so a `.cmd` or `.bat` stub is never found (WinError 2). The child
      is `[sys.executable, "-c", "from niceclaude import cli; cli.subprocess.run
      = <fake>; raise SystemExit(cli.main(['refresh']))"]`. That is the same
      fake shape `test_windows_regressions` uses in-process.
    - **It asserts** that `state.json` lands under `accounts/<slug>/`.
  - `niceclaude paths`, run in a subprocess under config dirs A and B, prints
    different `data_dir` values, and each matches `account_paths()`.
    The child's environment:
    - drops `NICECLAUDE_DIR`, which conftest sets, and which under D3 would
      give A and B the same `data_dir`;
    - redirects `HOME`, `USERPROFILE` and `LOCALAPPDATA` to `tmp_path`, as the
      end-to-end test does.

    The expected values come from `account_paths()`, called with that same
    environment.
  - `niceclaude paths` prints exactly the ten keys in §5, *Paths*.
  - `niceclaude paths pid_path` prints the same value as the JSON's
    `pid_path`, on one line. An unknown key exits nonzero.
  - `account_paths` returns `slug`, which is `""` for the default account.
- **Existing tests:**
  - Keep green, unedited: the whole suite. Conftest's `NICECLAUDE_DIR` pins
    `DATA_DIR` and `POLICY_PATH` under D3, which is what proves D3.
  - Watch in particular: `test_install_merge` (the `CLAUDE_CONFIG_DIR` cases),
    `test_exempt`, `test_windows_regressions` (which reads `cli.SETTINGS_PATH`),
    and `smoke_installed.py`.

### Phase 2 — the stamp, the guard, and the log filter

- **Scope:**
  - `publish_state` and `sample_once` write `config_key`.
  - Add `hook.load_state(path, key)` (D6), used at both load sites in `run`
    and in `cmd_status`. `run`, not `load_state`, logs `foreign snapshot` once
    per invocation.
  - D10, if Phase 0 shows it is needed: the payload fallback, the rebinding of
    the globals in `main()` (including `CONFIG_DIR_OVERRIDE`), and
    `refresh_snapshot`'s use of the override.
  - The D7 legacy rule.
  - `cmd_status` flags a foreign snapshot rather than judging buckets from it.
  - `cmd_status` also gives the two upgrade warnings, (a) and (b), exactly as
    D9 specifies them. They apply only when `DATA_DIR != ROOT_DIR`. For (b),
    that means a live root pid *and* a root snapshot carrying no `config_key`
    at all.
  - **Where the warnings go.** `cmd_status` returns early at
    `matched is None`, for an unpaced folder. The warnings go in the header,
    after the `hook:` line and before that early return, so an unpaced folder
    still shows them. Phase 4 puts its account-level lines in the same place.
  - The `load_log` filter (D11), with its stderr line.
- **Files:**
  - `src/niceclaude/hook.py` and `src/niceclaude/cli.py`. `plot.py` needs no
    change: `plot.collect(records, ...)` is handed records from `cli.load_log`,
    so the filter covers it.
  - New tests: `tests/test_account_stamp.py` and `tests/test_log_filter.py`.
  - Edits to existing tests: `tests/conftest.py` gains an autouse fixture that
    pins `ACCOUNT_KEY` to `""` in `_shared`, `hook` and `cli`, and
    `hook.CONFIG_DIR_OVERRIDE` to `None`. A test opts out with the marker
    `real_account_key`.
  - The repo has no pytest ini section, so conftest registers the marker in
    `pytest_configure`, with
    `config.addinivalue_line("markers", "real_account_key: do not pin ACCOUNT_KEY to \"\"")`.
  - The new Phase 2 tests use `real_account_key`.
  - In the same change, Phase 2 adds
    `pytestmark = pytest.mark.real_account_key` to
    `tests/test_account_paths.py`. It asserts on the real import-time key,
    which the fixture would otherwise pin to `""`. In Phase 1 the file carries
    no marker, because neither the marker nor the fixture exists yet.
  - The same fixture pins
    `cli.REGISTRY_PATH` to
    `tmp_path / "accounts.json"`. Otherwise the `cmd_install` calls in
    `test_install_merge`, `test_exempt` and `test_subagent_cache_ttl` would
    pile registry entries up in conftest's persistent directory once Phase 4
    lands. `tests/smoke_installed.py` writes the matching
    `config_key` into the `state.json` it hand-builds, taken from
    `niceclaude paths`, so that the real hook does not run a real refresh.
- **New tests:** every new test sets the key explicitly, never inheriting it
  from the shell.
  - A foreign fresh snapshot makes `run` call `refresh_snapshot`
    (monkeypatched), at the first load site.
  - A foreign snapshot that is itself over the line, with a failing refresh:
    the result is `blind: True` and `hold: "hard"`, with reason
    `"no usable buckets in snapshot"` (or the distinct foreign reason, if D6's
    option is taken). The reason names no bucket from the foreign snapshot.
    This is the case that would otherwise brake "confidently".
  - A foreign snapshot with a successful refresh that writes an own-key
    snapshot: the decision uses the new snapshot, through the second load site.
  - An unstamped snapshot is trusted under the default key and refreshed under
    a non-default one.
  - The `foreign snapshot` line appears once per invocation, not once per
    chunk.
  - The unpaced gate still comes first: an unpaced folder with a foreign
    snapshot never refreshes (extends `test_hook_ordering`).
  - `load_state` returns:
    - `(state, None)` on a match;
    - `({}, "<key>")` on a foreign stamp;
    - `({}, "<unstamped>")` for a parsed snapshot with no stamp, under a
      non-default key;
    - `({}, None)` for a missing file, for an unparseable one, and for a
      parsed non-dict such as `[]`.
  - `status` on a present but unreadable `state.json` prints
    `snapshot: unreadable -- delete it or restart niceclaude watch`, and exits
    0 without a `KeyError`.
  - On a fresh account with no `state.json`, `run` writes no
    `foreign snapshot` line, and `status` still prints
    `no snapshot yet -- is \`niceclaude watch\` running?`.
  - On a foreign snapshot, `status` prints
    `snapshot: written by another account (<foreign>) -- ignored` and exits 0,
    without the `KeyError` its bucket table would otherwise raise on `{}`.
  - Warnings (a) and (b). The tests pin `cli.ROOT_DIR` and `cli.DATA_DIR` to
    two different subdirectories of `tmp_path`, and monkeypatch
    `cli.pid_alive`.
    - (a) fires on a fresh, unstamped root snapshot.
    - (b) fires when the root pid is live and the root snapshot has no
      `config_key`.
    - (b) does **not** fire when the root snapshot carries `config_key: ""`.
    - Neither fires when `DATA_DIR == ROOT_DIR`.
    - Both appear for an unpaced folder, which means before `matched is None`
      returns.
    - Both print the shared message word for word: *"a daemon holding the
      legacy pidfile has not published a stamped snapshot; ..."*.
  - A `usage.jsonl` with interleaved keys and unstamped records is filtered by
    D11:
    - under key A, it keeps A's records and every unstamped record, and skips
      B's;
    - under the default key, it keeps default-key records and every unstamped
      record.

    `check` raises no false "usage DECREASED" across the skipped records, and
    the stderr line `skipped N records from other accounts` carries B's count.
  - `status` does not write to `hook.log` on a foreign snapshot.
  - D10, if built:
    - Both transcript shapes, main-agent and subagent, yield the same config
      dir.
    - A path with no `projects` component yields none.
    - With the environment set, a disagreeing payload is logged and ignored,
      and `main()` leaves every monkeypatched hook path untouched.
    - With the environment empty and a transcript under a non-default config
      dir, the payload rebinds `hook.STATE_PATH`, `hook.HOOK_LOG_PATH` and
      `hook.CONFIG_DIR_OVERRIDE`, but not `POLICY_PATH`. This test:
      - sets `NICECLAUDE_DIR` to `tmp_path`, so the rebound paths land there
        and not in conftest's persistent directory;
      - stubs `hook.run`, so only `main()`'s rebinding runs;
      - pins `hook.POLICY_PATH` under `tmp_path` and writes a policy file
        there, because `main()` returns before any derivation when
        `POLICY_PATH` does not exist.
    - With the environment empty and a transcript under the default
      `~/.claude`, with both `USERPROFILE` and `HOME` redirected to
      `tmp_path` as in Phase 1's home test, `main()` rebinds
      nothing, and `CONFIG_DIR_OVERRIDE` stays `None`. This is U1's guard.
    - With `hook.CONFIG_DIR_OVERRIDE` set, `refresh_snapshot()`, still called
      with no arguments, passes `CLAUDE_CONFIG_DIR` to the child. Check this
      by monkeypatching `subprocess.run` and asserting on its `env`. With the
      override `None`, it passes no `env` at all.
    - The seven existing zero-argument `refresh_snapshot` replacements still
      work.
- **Existing tests** that must stay green under the new fixture, unedited:
  - the hand-written snapshot users `test_band`, `test_hook_ordering` (for
    example `test_paced_folder_with_fresh_snapshot_also_avoids_refresh`) and
    `test_max_delay`;
  - the `cmd_status` users `test_band_cli`, `test_exempt`, `test_wait_report`
    and `test_subagent_cache_ttl` (committed in 543dd4d);
  - the `load_log` users `test_sampling_health`, `test_usage_jitter` and
    `test_plot`.

  Without the fixture, every one of them breaks: conftest always sets a
  non-default `CLAUDE_CONFIG_DIR`, so under D7 all their unstamped snapshots
  would count as foreign.

### Phase 3 — the account identity (diagnostic)

- **Scope:**
  - Read `oauthAccount.{accountUuid, organizationUuid}` from the global config
    at the location Phase 0 confirmed, in the CLI only.
  - Stamp `account` into records and state.
  - `check` reports a UUID change within one directory and a UUID shared across
    directories.
- **Files:** `src/niceclaude/cli.py`, and `tests/test_account_identity.py`.
- **New tests:**
  - A fixture config with a fake `oauthAccount`, including `emailAddress` and
    `fullName`. Assert that neither value appears anywhere in `state.json`,
    `usage.jsonl`, or captured stdout and stderr.
  - A missing file, malformed JSON, and no `oauthAccount` (the API-key user)
    all give `account: None` without raising.
  - `projects` keys that differ only by case still yield the account pair.
  - A genuinely duplicated `oauthAccount` yields the last value without
    raising.
  - `check` flags a mid-log UUID change.
  - `check` flags two account directories whose `state.json` files carry the
    same `account`. This test lays out `ROOT_DIR/state.json` and
    `ROOT_DIR/accounts/*/state.json` under `tmp_path`, and reads no logs.
  - The hook module never opens the global config. Assert it with a
    monkeypatched `open` while `run` executes.
- **Existing tests:**
  - Keep green, unedited: `test_windows_regressions`, which fakes
    `sample_once`'s `subprocess.run`.
  - **The identity reader resolves its file at call time**, exactly as
    `claude_settings_path()` does: `<CLAUDE_CONFIG_DIR>/.claude.json` when
    `os.environ.get("CLAUDE_CONFIG_DIR")` is set, else `~/.claude.json`. It is
    never derived from `ACCOUNT_KEY`. Conftest's `CLAUDE_CONFIG_DIR` redirect
    therefore keeps every in-process test off the developer's real file.
  - Keep green, unedited: `test_sampling_health` and `test_usage_jitter`. They
    write `usage.jsonl` directly, and their records carry no `account`, so the
    identity checks in `check` must treat a missing `account` as unknown, not
    as a change.

### Phase 4 — registry, UX, and docs

- **Scope:**
  - The registry: `install` records its account in `REGISTRY_PATH` with
    `write_atomic`, last writer wins. `uninstall` sets `"hook": false` on that
    entry rather than dropping it. Both compute the key from `os.environ` at
    call time (see *Behaviour after the change*).
  - `status` prints the following in the header, after the `hook:` line and
    before the `matched is None` early return, so an unpaced folder still shows
    them:
    - an `account:` line;
    - the legacy-history hint, under D9's conditions and with its two
      fully resolved `mv` lines;
    - a relative-path warning (D4);
    - every known account, as *Behaviour after the change* specifies, with
      daemon liveness.
  - Help text: `install --force` names the accounts it affects.
  - `deploy/`:
    - a templated `niceclaude@.service`, with no `ExecStopPost`;
    - a `-ConfigDir` parameter for `niceclaude-task.ps1`. Today the script
      registers a fixed `$TaskName = 'niceclaude-watch'` with `-Force`, and
      `New-ScheduledTaskAction` carries no environment. So a second account
      would silently replace the first. With `-ConfigDir`:
      - the task name becomes `niceclaude-watch-<slug>`, with the slug taken
        from `niceclaude paths slug`, run with `CLAUDE_CONFIG_DIR` set. An
        empty slug (a `-ConfigDir` that is the default account) keeps the plain
        `niceclaude-watch` name;
      - the action runs `cmd.exe /c set "CLAUDE_CONFIG_DIR=<dir>" &&
        "<niceclaude.exe>" watch --interval N`;
      - the script's header notes that stopping a slugged task's daemon with
        `niceclaude stop` needs that `CLAUDE_CONFIG_DIR` exported in the shell.

      Without `-ConfigDir`, the script is unchanged;
    - an entrypoint that takes the pidfile path from
      `niceclaude paths pid_path`, the single-value form, with no JSON parsing
      in shell.
  - Fix the stale `deploy/README.md` line that says `NICECLAUDE_DIR` relocates
    data only, which contradicts decision 16.
  - Docs:
    - update the "State on disk" section of `harness/README.md`;
    - add a `design-decisions.md` entry recording D1–D11;
    - document the rejoin path for `NICECLAUDE_DIR` users (Q5):
      1. stop the daemon;
      2. drop `NICECLAUDE_DIR` from the alias;
      3. copy `policy.json` to the root, or re-run `on` for each rule;
      4. restart `watch`.

      History stays where it is unless it is moved as D9 describes;
    - update CHANGELOG, including D9's upgrade warning word for word, and
      README.
- **Files:** `src/niceclaude/cli.py`, `deploy/*`, `harness/README.md`,
  `harness/design-decisions.md`, `README.md`, `CHANGELOG.md`, and
  `tests/test_accounts_registry.py`.
- **New tests** (`tests/test_accounts_registry.py` pins `REGISTRY_PATH` under
  `tmp_path`):
  - `install` under two `CLAUDE_CONFIG_DIR` values in one process writes both
    registry entries, each under its own key despite the autouse fixture, and
    is idempotent.
  - `uninstall` flips `hook` to `false` without dropping the entry.
  - For a registered account, `status` reads hook state live from
    `<config_dir>/settings.json`, and shows a stored `hook: false` as
    `(uninstalled on purpose)`.
  - An `accounts/<slug>` directory with no entry shows as
    `no install recorded for <slug>`.
  - The legacy-history hint appears only under D9's four conditions. It prints
    one resolved `mv` per existing file, and never names `state.json`,
    `policy.json`, the marker, or the registry.
  - The help pages carry the new text.
- **Existing tests:**
  - Keep green: `test_help`. It does not assert `install`'s wording, but the
    new `--force` text must still fit in 79 columns.
  - Keep green: `test_install_merge`. Its round-trip test must still restore
    Claude's `settings.json` exactly, and the registry lives in `ROOT_DIR`, not
    in that file.
  - Keep green: `test_exempt`, if `describe_installation` wording changes.

The former Phase 5, one daemon for every account, is dropped from this plan
(Q4). It survives only as the future idea in §4 D.

---

## 9. Interaction with the subagent cache-TTL work (543dd4d)

**`CLAUDE_SETTINGS_MARKER_PATH` stays at `ROOT_DIR`, shared, exactly where it
is today.** It must not follow `DATA_DIR`.

The marker is already account-aware on its own terms. `load_ttl_marker`
returns a map from Claude settings path to the TTL written there, keyed by
`_marker_key`, so one file serves every `CLAUDE_CONFIG_DIR`. That is the point
of its docstring, and `test_installs_under_two_config_dirs_keep_separate_records`
pins it.

Moving it per account would strand keys that were set before the upgrade.
Suppose `CLAUDE_CONFIG_DIR=~/.claude-work niceclaude install --subagent-cache-1h`
ran before the upgrade and wrote `<root>/claude_settings_marker.json`. After the
upgrade, that shell's `uninstall` would look in `accounts/<slug>/`, find no
marker, and `release_subagent_ttl` would report "niceclaude did not set it",
leaving `subagentPromptCacheTtl` behind for good.

The policy keys `subagent_cache_ttl_written` and `subagent_ttl_orphans` need no
change. They are keyed by folder, and live in the shared policy.

The TTL work has since been committed as 543dd4d. This plan builds on it, and
§9 describes it as committed there.

---

## Review log

Two reviews. Every finding was checked against the code or the machine before
it was applied, and every one was accepted, so there are no rebuttals. Where a
check turned up something extra, it is noted.

### Review 1: F1–F13

Review 2 confirmed that all thirteen were applied correctly.

- **F1 — Phase 2 broke the suite.** Accepted. `conftest.py` `setdefault`s a
  non-default `CLAUDE_CONFIG_DIR`, and `smoke_installed.py` sets one and
  hand-writes an unstamped snapshot. The fix is the autouse fixture, a stamped
  `smoke_installed.py`, `paths` moved to Phase 1, and a per-phase list of the
  existing tests each phase touches. Only `smoke_installed.py` spawns real
  processes, so it is the only suite file the fixture cannot cover.
- **F2 — MSYS translation.** Accepted. Measured here: an exported
  `NC_TEST_X=/c/Users/x` arrived in native Python as `C:/Users/x`, and
  `realpath('/c/Users/Joshu')` gave `F:\c\Users\Joshu`.
- **F3 — `--config-dir`.** Accepted as a Phase 0 check and as D10. The local
  `claude --help` (2.1.282) does not list the flag, so Phase 0 first confirms
  that it exists. The `transcript_path` fallback is worth having either way.
- **F4 — old daemons.** Accepted. With the variable unset, `PID_PATH` is at the
  legacy root, so a plain-shell `stop` works. The real hole is a work-account
  daemon writing unstamped snapshots into the root, which D9 and Phase 2's
  warnings now cover.
- **F5 — the log still mixes.** Accepted, as D11 plus tests.
- **F6 — the wrong reason string.** Accepted. In `decide`, an empty `buckets`
  leaves `enforced` empty, which returns `"no usable buckets in snapshot"`.
  `run` does load the state at two sites, hence `load_state()`.
- **F7 — duplicate keys.** Accepted. Measured with `object_pairs_hook`: zero
  exact duplicates, and six case-variant `projects` keys.
- **F8 — the `hashlib` number.** Accepted in substance. Re-measured three times
  on CPython 3.13.7 here: 19.4, 25.6 and 37.2ms cumulative, against the
  reviewer's 8.5–10.8ms, so the plan gives the range. Either way it is several
  times `zlib` and a large fraction of the hook, and the conclusion stands. The
  lazy-import condition and the scrubbed child environment are adopted.
- **F9 — the collision argument.** Accepted. D3 never slugs a set
  `NICECLAUDE_DIR`, so the collision could not happen. The `accounts/` layout
  is kept for tidiness and enumeration.
- **F10 — the stamp was overstated.** Accepted. §3 point 4 now lists only the
  cases where two accounts share one directory, and D4 makes the key follow
  Claude.
- **F11 — the registry nagged forever.** Accepted: `"hook": false` on
  uninstall, systemd `ExecStopPost` dropped, the `%i` form, and `write_atomic`
  with last writer wins. `cmd_watch`'s `finally` does remove the pidfile.
- **F12 — downgrade.** Accepted, in D9, along with the two-daemon overlap.
- **F13 — option E.** Accepted as an option and rejected as the design (§4 E):
  it fixes only the hook, gives one path two sources of truth, forces
  per-account policy, and writes one more key into a file niceclaude does not
  own.

### Review 2: R1–R10

- **R1 — the TTL marker stays at the root.** Accepted. In the working tree,
  `load_ttl_marker` returns a settings-path-keyed map, and
  `release_subagent_ttl` reports "niceclaude did not set it" when it finds no
  entry. `test_installs_under_two_config_dirs_keep_separate_records` switches
  `CLAUDE_CONFIG_DIR` between installs. The "has moved since" wording I cited
  in §9 came from an earlier revision of `read_ttl_marker`; its docstring now
  reads "CLAUDE_CONFIG_DIR pointed elsewhere then". §5 and §9 are fixed, and
  Phase 1 tests that the marker path is the same in every case.
- **R2 — subagent transcripts.** Accepted. Both shapes exist on this machine;
  for example there is a `<session>/subagents/agent-<id>.jsonl` under
  `~/.claude/projects/F--Josh-solidState/`. D10 now walks up to the nearest
  `projects`, the environment wins, and Phase 0 records what a subagent's hook
  receives.
- **R3 — D10's mechanism.** Accepted: `account_paths()`, globals rebound in
  `main()`, and `refresh_snapshot(config_dir)`. `test_band`, `test_max_delay`
  and `test_hook_ordering` do pin `hook.STATE_PATH`, `hook.POLICY_PATH` and
  `hook.HOOK_LOG_PATH`.
- **R4 — `load_state` arguments.** Accepted. `test_band_cli`, `test_wait_report`
  and `test_exempt` pin `cli.STATE_PATH` only, never `hook.STATE_PATH`.
- **R5 — `load_log`'s signature.** Accepted. `test_plot` does
  `monkeypatch.setattr(cli, "load_log", lambda: [])`.
- **R6 — `paths` help.** Accepted. `test_help` asserts at least three
  description lines, every line within 79 characters, and help on every
  argument.
- **R7 — watch and stop are untested.** Accepted. Searching `tests/` for
  `cmd_watch`, `cmd_stop`, `PID_PATH`, `read_pid` and `_watch_loop` returns
  nothing. Phase 5's claim is fixed, and Phase 1 gains pidfile tests.
- **R8 — the registry key at call time.** Accepted. `test_subagent_cache_ttl`
  calls `setenv("CLAUDE_CONFIG_DIR", ...)` several times in one test.
  `REGISTRY_PATH` is now a `_shared` constant.
- **R9 — the Phase 3 labels.** Accepted. Only `test_windows_regressions`
  monkeypatches `cli.subprocess.run`. `test_sampling_health` and
  `test_usage_jitter` pin `cli.LOG_PATH` and write records directly. The
  wording is fixed, and there is a note that records with no `account` must
  not count as a change.
- **R10 — the slug length.** Accepted. 32 + 1 + 8 = 41.

### Review 3: S1–S13

The reviewer spot-checked F1, F6, F8, R1 and R3–R8, and all of them hold. The
orchestrator settled Q1–Q5 while the user was away, with two independent
reviewers in agreement, and §7 records each as a **Decision:** line.

- **S1 — unstamped history (decided: adopt).** Accepted. Under the old D11, a
  migrated work account, or a `NICECLAUDE_DIR` user whose records are all
  unstamped, would see no history at all under their non-default key. That
  contradicts D9's `mv` advice and Q5. D11 now keeps unstamped records under
  every key, and explains why that is looser than D7.
- **S2 — the rebind trigger.** Accepted. Under the Phase 2 fixture,
  `ACCOUNT_KEY` is `""` while conftest's `CLAUDE_CONFIG_DIR` is non-default, so
  a "differs from import-time" trigger would fire in every test that calls
  `main()`. D10 now rebinds only when the environment is unset and the payload
  supplied a config dir.
- **S3 — the Windows stub.** Accepted. `sample_once` calls
  `subprocess.run(["claude", "-p", "/usage"], ...)` with no shell. Without a
  shell, Windows does not search `PATHEXT` for a `.cmd` or `.bat`. The child is
  now a `sys.executable -c` that fakes `cli.subprocess.run` in-process.
- **S4 — the settled decisions.** Accepted. Phase 5 is deleted, §4 D is marked
  as a future idea, the rejoin path is in Phase 4 Docs, and Phase 0 now
  confirms D10's precedence rather than deciding it.
- **S5 — the migration hint.** Accepted. D9 gives the four conditions, the
  "only if that history was recorded by this account alone; a mixed log cannot
  be split" caveat, and one resolved `mv` per file, and names what is never
  moved. "One mv" is corrected to two.
- **S6 — the `load_state` contract.** Accepted. `cmd_status` computes
  `age = int(now) - st["ts_epoch"]` unconditionally (read at HEAD), so `{}`
  would raise `KeyError`. It now returns early with the "ignored" line.
- **S7 — Phase 0 is runnable (decided: adapt).** Accepted. A scratch config dir
  has no login, so no hook fires in it. Procedure A runs a probe via
  `--settings` on the real account, with the variable unset. Procedure B is
  marked "needs the user". The assumptions in force until B runs are listed. No
  credential files are copied.
- **S8 — the upgrade warnings.** Accepted, verbatim, in D9. Under the default
  key, `DATA_DIR == ROOT_DIR`, so both warnings would be meaningless there.
  Hence the guard.
- **S9 — what the registry can mean.** Accepted. `describe_installation`
  already recognizes the `--settings` fragment as a separate source of a hook,
  and slugs are one-way hashes. So the entry shape, the live evaluation and
  the "no install recorded" line are adopted.
- **S10 — "no I/O" overstated.** Accepted. `realpath` measured 0.32ms here
  earlier, and the reviewer measured `norm_path` at 0.12ms. The plan quotes
  0.12–0.3ms, and adds the short-circuit and the cached default.
- **S11 — the slug rule.** Accepted: five explicit steps, and a pinned example
  with mixed case and a space.
- **S12 — home from `env`.** Accepted. `os.path.expanduser` reads `USERPROFILE`
  on Windows and `HOME` on POSIX, both from `os.environ`, so it ignores a
  passed-in `env`.
- **S13 — the tautological pidfile test.** Accepted. `PID_PATH` is defined as
  `os.path.join(DATA_DIR, "daemon.pid")`, so "`PID_PATH` is under `DATA_DIR`"
  could not fail. The test now compares `data_dir` and `pid_path` across A and
  B, and compares `niceclaude paths` across subprocesses. `pid_path` is added
  to `account_paths`.

### Review 4: T1–T13

The reviewer spot-checked S3, S6, S10, S12, S13, R4, R5, R7 and F6, and all of
them hold. There were four blockers (T1–T4) and nine minor findings. All were
checked and accepted, so there are no rebuttals.

- **T1 — `refresh_snapshot`'s signature (blocker).** Accepted. A grep of
  `tests/` finds seven replacements of `hook.refresh_snapshot`: `test_band`
  (four), `test_max_delay` (one), and `test_hook_ordering` (two). Each is a
  zero-argument callable, such as `lambda: False` or `boom`. D10 now keeps the
  zero-argument signature and uses `hook.CONFIG_DIR_OVERRIDE`. The R3 entry
  above, which says `refresh_snapshot(config_dir)`, is superseded by this.
- **T2 — warning (b) false positive (blocker).** Accepted. After the upgrade,
  the default account's new daemon owns `ROOT_DIR/daemon.pid`, and its
  `publish_state` stamps `config_key: ""`. So (b) now also requires the root
  snapshot to have no `config_key` at all. A test covers the `""` case.
- **T3 — absent is not foreign (blocker).** Accepted. Today `cmd_status`
  prints `no snapshot yet -- is \`niceclaude watch\` running?` when
  `STATE_PATH` is missing. `load_state` now returns `({}, None)` for a missing
  or unparseable file.
- **T4 — the Windows task collides (blocker).** Accepted. In
  `deploy/niceclaude-task.ps1`, `$TaskName = 'niceclaude-watch'` is fixed, it
  is registered with `-Force`, and `New-ScheduledTaskAction` takes no
  environment. Phase 4 now names the task by slug and runs a
  `cmd.exe /c set ... &&` action.
- **T5 — the `cmd_uninstall` message.** Accepted. §5 now names it as the
  exception.
- **T6 — `NICECLAUDE_DIR` in the `paths` subprocess.** Accepted. Conftest
  `setdefault`s it, and D3 would then make A and B identical.
- **T7 — naming.** Accepted. `account_paths` returns `config_key`, and
  `niceclaude paths` has a fixed set of ten JSON keys, pinned by a test.
- **T8 — where the `status` lines go.** Accepted. `cmd_status` prints
  `hook:` and then returns at `matched is None`. The account-level output now
  goes between the two.
- **T9 — call-time paths in the warning tests.** Accepted: the warnings
  compute their paths at call time, and the tests pin `cli.ROOT_DIR`,
  `cli.DATA_DIR` and `cli.pid_alive`. The redundant "never under the default
  key" clause is replaced by the `DATA_DIR == ROOT_DIR` case.
- **T10 — the source for a shared UUID.** Accepted in its second form. `check`
  reads `account` from the root and per-account `state.json` files, never from
  logs, and a test is added.
- **T11 — D4 against S12.** Accepted. `norm_path` calls `expanduser`, which
  reads `os.environ`. D4 now expands `~` against the `env` home first, which
  makes `expanduser` a no-op.
- **T12 — `test_help`.** Accepted. `test_help` checks line width, description
  length and argument help. It does not check `install`'s wording, so it is
  "keep green", not "edit".
- **T13 — the registry leaks from tests.** Accepted. The Phase 2 autouse
  fixture also pins `cli.REGISTRY_PATH` under `tmp_path`.

### Review 5: U1–U7

The reviewer confirmed that T1–T4 are fixed, and marked the plan not ready,
with one blocker and six minor findings. All seven were checked and accepted,
so there are no rebuttals.

- **U1 — D10 would override the default account (blocker).** Accepted. With
  `CLAUDE_CONFIG_DIR` unset, every default-account `transcript_path` lies under
  `~/.claude/projects`, so the old Mechanism would always have set
  `CONFIG_DIR_OVERRIDE`, and changed today's refresh environment for every
  default user. That is the unverified Q2 risk, applied to everyone. Now a
  derived `config_key` of `""` rebinds nothing, and a test guards it.
- **U2 — the opt-out marker.** Accepted. `test_account_paths` asserts on the
  import-time key, which the autouse fixture pins to `""`, so it is opted out
  at module level.
- **U3 — non-dict and unreadable snapshots.** Accepted. `json.load` can return
  a list or a scalar, and `cmd_status` would then index `st["ts_epoch"]`. A
  non-dict now counts as unparseable, and `status` has an explicit
  `unreadable` line and exits 0.
- **U4 — pins for the D10 tests.** Accepted. The fixture also pins
  `hook.CONFIG_DIR_OVERRIDE`. The empty-environment test sets `NICECLAUDE_DIR`
  to `tmp_path` and stubs `hook.run`.
- **U5 — parsing in the entrypoint.** Accepted. `deploy/docker-entrypoint.sh`
  is `#!/bin/sh`, and its header requires dash and busybox ash compatibility.
  There is no `jq` there, so `niceclaude paths <key>` prints one bare value.
- **U6 — the slug in one place.** Accepted. `account_paths` returns `slug` and
  `log_path`, and `paths` adds the three root-level keys. An empty slug keeps
  the plain task name, and the ps1 header notes what `stop` needs.
- **U7 — warning (b)'s wording.** Accepted. A default-account daemon that is
  logged out holds the root pidfile, but leaves the old unstamped snapshot in
  place, so (b) cannot tell it from an old daemon. The message is now
  conditional, and the Phase 2 test strings and CHANGELOG reference follow
  D9's wording.

### Review 6: V1–V6

The reviewer confirmed that U1–U7 hold, and marked the plan not ready, with
one blocker and five minor findings. All six were checked and accepted, so
there are no rebuttals.

- **V1 — the default-dir cache went stale in tests (blocker).** Accepted.
  `conftest.py` `setdefault`s a non-default `CLAUDE_CONFIG_DIR` before any
  import, so a per-process cache is filled from the developer's real home
  before any test redirects `HOME` or `USERPROFILE`. D2 now keys a one-entry
  cache on the home string, and the hook still pays at most one `norm_path`
  per process.
- **V2 — the baseline was wrong.** Accepted and re-measured:
  `uv run --with pytest pytest -q` at 543dd4d gives `499 passed`.
- **V3 — the policy gate.** Accepted. `hook.main()` returns 0 when
  `os.path.exists(POLICY_PATH)` is false, before `run`. D10 now puts the
  derivation after that gate, and the rebind test writes a policy file under a
  pinned `hook.POLICY_PATH`.
- **V4 — marker registration.** Accepted. `pyproject.toml` has no
  `[tool.pytest]` section, and there is no `pytest.ini`, `setup.cfg` or
  `tox.ini`. So conftest registers `real_account_key` in `pytest_configure`,
  and that name is now used throughout.
- **V5 — the `paths` A/B test home.** Accepted. The child's `HOME`,
  `USERPROFILE` and `LOCALAPPDATA` are redirected to `tmp_path`, and the
  expected values come from `account_paths()` with the same environment.
- **V6 — a realpath on every call.** Accepted. The disagreement check
  compares `normcase(normpath())` only, and accepts that a symlinked spelling
  may log a false disagreement.

### Review 7: W1–W6

Verdict: READY, with no blockers, and V1–V6 verified. All six minor findings
were applied.

- **W1 — a slug under `NICECLAUDE_DIR` (orchestrator decision).** Applied.
  `slug` is computed from any non-empty key, even when `NICECLAUDE_DIR` is set;
  only `data_dir` ignores it (D3). So `zlib` is imported lazily, and only for
  a non-empty key. The scrubbed-subprocess test uses the default account, so
  it is unaffected.
- **W2 — where the identity reader looks.** Applied. The reader resolves
  `<CLAUDE_CONFIG_DIR>/.claude.json`, else `~/.claude.json`, at call time, the
  same way `claude_settings_path()` does. It never derives the file from
  `ACCOUNT_KEY`. The "honour a redirected `HOME`" sentence was wrong, because
  `test_windows_regressions` redirects no `HOME`, and is removed.
- **W3 — when the marker lands.** Applied. `test_account_paths.py` has no
  marker in Phase 1. Phase 2 adds `pytestmark` in the same change that
  registers the marker and the fixture.
- **W4 — D2's short-circuit.** Applied. Equal normalized forms mean the
  default, with no `realpath`. Unequal ones settle nothing, so both sides go
  through `norm_path`, and the default's comes from the cache.
- **W5 — the U1 guard test's home.** Applied: both `USERPROFILE` and `HOME`
  are redirected to `tmp_path`.
- **W6 — the root in `account_paths`.** Applied. It resolves the root from
  `env` (`NICECLAUDE_DIR`, else `LOCALAPPDATA` on `nt`, else the `env` home),
  and never reads the import-time `ROOT_DIR`.
