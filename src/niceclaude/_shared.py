"""Paths and constants shared by the hook and the CLI.

Imports `os` and nothing else, bar `zlib`, which account_slug imports lazily
and only for a non-default account. The hook is on the hot path for every tool
call in every agent and subagent, so anything it imports transitively is a tax
paid thousands of times a night. Measured: stdlib-only hook is 16ms, the same
module plus argparse/re/subprocess is 29ms.
"""

import os

HOME = os.path.expanduser("~")


def _env_home(env):
    """The home directory as `env` describes it, falling back to HOME.

    This is expanduser's own lookup -- USERPROFILE on Windows, HOME elsewhere --
    but read from `env` rather than from the process. expanduser only ever
    consults os.environ, so a resolver handed a test's environment would
    otherwise quietly build its defaults from the developer's real home. The
    per-platform order keeps the import-time result identical to HOME, which is
    what every path was built from before `env` existed.
    """
    if os.name == "nt":
        return env.get("USERPROFILE") or HOME
    home = env.get("HOME")
    if home:
        # posixpath.expanduser strips trailing slashes, but keeps a HOME of "/"
        # as "/" rather than "". Match both, or HOME=/home/x// would move every
        # path from where it was before `env` existed.
        home = home.rstrip("/") or "/"
    return home or HOME


def _root_dir(env=os.environ):
    """The shared data root: what the data dir was before accounts existed.

    NICECLAUDE_DIR wins outright and verbatim, which keeps the documented
    one-directory workaround and the test suite's redirection exactly as they
    were.
    """
    override = env.get("NICECLAUDE_DIR")
    if override:
        return override
    home = _env_home(env)
    if os.name == "nt":
        base = env.get("LOCALAPPDATA") or os.path.join(home, "AppData", "Local")
        return os.path.join(base, "niceclaude")
    return os.path.join(home, ".local", "share", "niceclaude")


def _config_dir(env=os.environ):
    override = env.get("NICECLAUDE_CONFIG_DIR")
    if override:
        return override
    # If the data dir has been relocated, keep config alongside it. Otherwise a
    # container would need two bind mounts to persist state, and the second one
    # is easy to forget -- losing settings.json silently unpaces everything.
    data_override = env.get("NICECLAUDE_DIR")
    if data_override:
        return os.path.join(data_override, "config")
    home = _env_home(env)
    if os.name == "nt":
        base = env.get("APPDATA") or os.path.join(home, "AppData", "Roaming")
        return os.path.join(base, "niceclaude")
    return os.path.join(home, ".config", "niceclaude")


def norm_config_dir(value):
    """Canonical form of a CLAUDE_CONFIG_DIR value, as Claude Code resolves it.

    Deliberately NOT norm_path: there is no expanduser. Claude does not expand a
    literal `~` in the variable -- it treats `~/.claude` as relative, and makes
    `<cwd>/~/.claude` (harness/platform-findings.md section 15) -- so expanding it
    here would key a cwd-relative directory as the default account. The key has
    to follow Claude, because no stamp can catch niceclaude and Claude
    disagreeing about which directory is meant. realpath makes a relative value
    absolute against the process cwd, as Claude does, and resolves symlinks;
    normpath and normcase fold trailing separators, slash direction and, on
    Windows, case.
    """
    return os.path.normcase(os.path.normpath(os.path.realpath(value)))


# One entry, keyed by the home the default was built from: {home: normalized
# <home>/.claude}. Keyed by home rather than filled once per process because the
# suite redirects the home per test, and conftest sets a non-default
# CLAUDE_CONFIG_DIR before anything is imported -- a per-process cache would be
# filled at import from the developer's real ~/.claude and never refreshed. In
# the hook the home never changes, so this is still one realpath per process.
_default_cache = {}


def config_key(config_dir, home):
    """The account key for `config_dir`: "" for the default account, else its
    norm_config_dir form.

    Unset, empty, and anything that resolves to `<home>/.claude` are all the
    default, so someone exporting CLAUDE_CONFIG_DIR=~/.claude in a profile keeps
    their history. A set-to-default value is the same login and the same usage,
    though Claude then reads `$CLAUDE_CONFIG_DIR/.claude.json` rather than
    ~/.claude.json (platform-findings section 15); only the usage matters here.

    The cheap comparison comes first: equal after normcase(normpath()) means the
    default with no realpath at all. Unequal settles nothing, since a symlinked
    spelling may still be the default, so both sides are then resolved -- the
    config dir's result is the key anyway, and the default's comes from the
    cache.
    """
    if not config_dir:
        return ""
    default = os.path.join(home, ".claude")
    fold = os.path.normcase(os.path.normpath(config_dir))
    if fold == os.path.normcase(os.path.normpath(default)):
        return ""
    key = norm_config_dir(config_dir)
    cached = _default_cache.get(home)
    if cached is None:
        cached = norm_config_dir(default)
        _default_cache.clear()
        _default_cache[home] = cached
    # ASSUMPTION (Phase 0, unverified): "resolves to the default dir" means
    # "the default login". Verified on Windows, where the credentials live in
    # the config dir; on macOS the keychain entry may be keyed on the
    # directory's spelling, which would make this the same dir but not the same
    # login.
    return "" if key == cached else key


_SLUG_CHARS = frozenset("abcdefghijklmnopqrstuvwxyz0123456789._-")


def account_slug(key):
    """The directory name under `accounts/` for account key `key`.

    A readable basename plus the CRC32 of the whole key, so two config dirs
    with the same basename still differ, and the name says which account it is.
    The basename is lowercased, stripped of leading dots, has every character
    outside [a-z0-9._-] replaced by `-`, has runs of `-` collapsed, and is capped
    at 32 characters: `.../My Claude  Work` becomes `my-claude-work-<crc>`.

    zlib, never hashlib: a cold hashlib import measured 9-37ms against a 16ms
    hook, zlib under 0.2ms. And zlib only for a non-empty key, so the default
    account's hot path imports nothing new. A CRC collision among a handful of
    accounts is not a practical concern, and the full-key stamp in state.json
    turns one into refresh churn rather than mis-pacing.
    """
    if not key:
        return ""
    import zlib  # lazy: see above
    crc = format(zlib.crc32(key.encode("utf-8", "surrogatepass")), "08x")
    out = []
    for ch in os.path.basename(key).lower().lstrip("."):
        ch = ch if ch in _SLUG_CHARS else "-"
        if ch == "-" and out and out[-1] == "-":
            continue
        out.append(ch)
    # Stripped after the cap: an unsafe last character, or a cut just after a
    # dash, would otherwise meet the separator below as a run of two.
    base = "".join(out)[:32].strip("-")
    # A basename with nothing left in it (a drive root, or all dots) would
    # otherwise give a name that starts with the separator.
    return f"{base}-{crc}" if base else crc


def account_paths(config_dir, env=None):
    """The account-scoped paths for a Claude config dir.

    Returns config_key, slug, data_dir, state_path, log_path, hook_log_path and
    pid_path. The import-time constants below are exactly this for the
    environment's own CLAUDE_CONFIG_DIR; it exists as a function so the CLI and
    the tests can ask about any other config dir.

    Everything comes from `env` (default os.environ), never from the import-time
    ROOT_DIR or the process home, so a test that hands it a redirected
    environment cannot end up comparing against the developer's real ~/.claude.

    The default account keeps the root itself as its data dir, so an upgrade
    moves nothing for a single-account user. Any other account gets
    `<root>/accounts/<slug>`, and `accounts/` keeps those out of the legacy
    file set and in one place to list. A set NICECLAUDE_DIR is used verbatim
    and never slugged -- it means exactly that directory -- but the key and the
    slug are still computed, because two accounts sharing it are told apart by
    the key rather than the path.
    """
    if env is None:
        env = os.environ
    key = config_key(config_dir, _env_home(env))
    slug = account_slug(key)
    root = _root_dir(env)
    if key and not env.get("NICECLAUDE_DIR"):
        data_dir = os.path.join(root, "accounts", slug)
    else:
        data_dir = root
    return {
        "config_key": key,
        "slug": slug,
        "data_dir": data_dir,
        "state_path": os.path.join(data_dir, "state.json"),
        "log_path": os.path.join(data_dir, "usage.jsonl"),
        "hook_log_path": os.path.join(data_dir, "hook.log"),
        "pid_path": os.path.join(data_dir, "daemon.pid"),
    }


ROOT_DIR = _root_dir()
CONFIG_DIR = _config_dir()

# ASSUMPTION (Phase 0, unverified): a hook inherits CLAUDE_CONFIG_DIR unchanged
# from the session that spawned it, so the environment alone names the account.
# Verified for the default dir only; a non-default dir, the
# CLAUDE_CODE_SUBPROCESS_ENV_SCRUB=1 case and `claude --config-dir` still need
# Procedure B. Until then the payload fallback (plan D10) stays unbuilt, and a
# hook that has lost the variable is keyed as the default account.
_ACCOUNT = account_paths(os.environ.get("CLAUDE_CONFIG_DIR"))
ACCOUNT_KEY = _ACCOUNT["config_key"]

# DATA_DIR is the account's own directory; everything account-scoped lives in
# it. ROOT_DIR holds what every account shares.
DATA_DIR = _ACCOUNT["data_dir"]
LOG_PATH = _ACCOUNT["log_path"]
STATE_PATH = _ACCOUNT["state_path"]
HOOK_LOG_PATH = _ACCOUNT["hook_log_path"]

# Shared by every account: policy is about folders, usage is about accounts, and
# a per-account policy would leave a folder unpaced under whichever account
# `on` was not run from.
POLICY_PATH = os.path.join(ROOT_DIR, "policy.json")
# Present only while `install --subagent-cache-1h/5m` has set Claude Code's
# `subagentPromptCacheTtl`: records which settings file and which value, so
# `uninstall` removes that key only when it is still ours and still unchanged.
# Already keyed by settings path, so one file at the root serves every account.
CLAUDE_SETTINGS_MARKER_PATH = os.path.join(ROOT_DIR,
                                           "claude_settings_marker.json")
# The same, for the CLAUDE_ASYNC_AGENT_STALL_TIMEOUT_MS that `install` puts in
# the settings `env` block: settings file -> the value written there. A file of
# its own, so a niceclaude that predates it still reads the TTL marker intact.
STALL_MARKER_PATH = os.path.join(ROOT_DIR, "claude_settings_stall_marker.json")
# Which config dirs `install` has been run against (written from Phase 4 on).
REGISTRY_PATH = os.path.join(ROOT_DIR, "accounts.json")
# Byte-identical for every account -- the hook command and its timeout -- so it
# is not keyed.
SETTINGS_PATH = os.path.join(CONFIG_DIR, "settings.json")

# Pace-line defaults. m0 is a starting grubstake -- the pure diagonal would
# permit 0% at 0% elapsed, so without it nothing could ever begin. m1 is an
# end-of-window reserve, so we come in under the wire rather than exactly on it.
DEFAULT_M0 = 5
DEFAULT_M1 = 8

# While braked, re-evaluate at least this often. This is also how long a policy
# change takes to reach an already-frozen agent, so it trades responsiveness
# against idle wakeups. Only ever runs while something is actually frozen.
DEFAULT_CHUNK = 15

# Extra reserve demanded of a SubagentStart, on top of m1. Spawning a fan-out
# commits to far more consumption than taking one more step in work already
# under way, so it is worth holding the spawn while still letting a running
# agent finish. 0 disables it; the two events then behave identically.
DEFAULT_FANOUT_RESERVE = 0

# Cap on a single hold, not on the total restraint: at the cap the hook releases
# while still over the line, the agent takes one step, and the next tool call
# brakes again. The point is the prompt cache -- a hold that outlives its TTL
# makes the next turn re-read the whole context from cold, so a wait taken to
# save budget can cost more than it saved. None means hold until the line
# catches up, which is the default because this is the one knob that
# deliberately lets work proceed while over the line.
DEFAULT_MAX_DELAY = None

# How far below the brake line the throttle line sits, in percentage points.
# The brake line keeps its exact meaning -- never above it -- and everything
# this adds lives below it, which is why it does not reopen the deadband
# question settled in harness/design-decisions.md #4.
#
# Its job is release hysteresis. Without it a hold ends the moment the line
# reaches pct+1, so you resume with under one quantum of headroom and the next
# 1% tick puts you over again: on the weekly line that is a 1.68h hold for
# every 1% of budget, and a cold prompt cache each time. A band means one hold
# buys a whole band's worth of running before the next one.
#
# 0 disables it, which is the previous behaviour exactly. The default is 7,
# which is wide enough that one hold buys a meaningful run: on the weekly line
# it is about 13h of headroom bought by a hold of the same order, against 1.68h
# bought by a hold of 1.68h with no band. It is well inside the 48h hook
# timeout, and it costs nothing from the ceiling -- the brake line is where it
# always was.
DEFAULT_BAND = 7

# What one hold costs while inside the band, in seconds. None means the band is
# pure hysteresis: you run through it at full speed and only the brake line
# stops you. Set it and the band becomes a lower gear instead -- each tool call
# holds this long, so you burn slowly and stay cache-warm rather than
# alternating long dark holds with full-speed bursts.
#
# The default is 180s, the lower gear rather than the free run, because a band
# crossed at full speed is spent almost immediately and the next hold is the
# full-length one the band was added to avoid. Three minutes is short against
# every prompt cache TTL, so the crawl stays warm.
DEFAULT_BAND_DELAY = 180

# How long one tool call may be held BLIND, in seconds: held with no release
# that data can justify -- no usable bucket, a snapshot too old to trust with
# the refresh failing, or every hot bucket over its floor with no reset clause
# to solve against (hook.decide marks all three `blind`). Past it the call goes
# through, logged `blind-release`, and the next tool call looks again.
#
# This is a deliberate exception to "never proceed on data we do not have",
# and the user's rule is what makes it one: never stall forever, fail free
# rather than freeze. Without it a blind hold had no ceiling but HOOK_TIMEOUT,
# i.e. two days, on an outage the hook could do nothing about.
#
# 240 rather than anything longer because the cost being bounded is the
# prompt cache, not the wait. A hold that outlives the 5-minute TTL makes the
# next turn re-read its context from cold, and an agent blind on every call
# would pay that on every call. Under the TTL, a long outage degrades to a
# slow, warm crawl. None is the old hold-until-seen behaviour, and 0 never
# holds blind at all. A confident brake -- one with a real wake time, stale or
# not -- is untouched: that is max_delay's business.
DEFAULT_BLIND_DELAY = 240

# What `install` registers as the hook's `timeout`, in seconds. This is the
# REAL ceiling on any hold: the harness kills the hook when it expires, the
# agent takes its tool call unpaced, and nothing is logged -- the killed process
# never reaches the release line in main(). An unmatched `brake` in hook.log is
# the only trace it leaves.
#
# It was 21600 (6h), and that was measurably leaking. In one 822-line hook.log:
# 198 unmatched brakes, the recent ones spaced 6.00-6.01h apart to the second,
# every one of them `week:Fable`, with the percentage climbing 41 -> 67 straight
# through them. The folder had `max_delay: null` set -- an explicit "never
# proceed while over the line" -- and the registered timeout was quietly
# converting that into `max_delay: 21600`.
#
# A hold is bounded by its window's own reset (bucket_pace clamps the wake), so
# the longest legitimate hold is one window: 7 days for the weekly buckets. 48h
# covers every hold seen in practice with room to spare -- the deepest overage
# in that log solved to ~15h -- without committing to a freeze measured in days.
#
# Do NOT simply drop the field to remove the ceiling: omitted, it defaults to
# 60s, which would cap every hold at one minute.
HOOK_TIMEOUT = 172800

MAX_STALE = 180      # a snapshot older than this is not trusted

# ...but while above the throttle line, only this long. Near the line the agent
# is released every `band_delay` and takes a step, and a step can burn a lot:
# trusting a 170s-old snapshot through several of them is how you sail past the
# brake line without ever seeing it. `/usage` costs no tokens
# (harness/platform-findings.md #26), so the only price of looking again is a
# couple of seconds of wall clock, and only when it matters.
NEAR_STALE = 15

# There is deliberately no self-imposed ceiling on a hold. There used to be
# (MAX_BRAKE, 6h) on the reasoning that by then every window has rolled -- which
# is false: the weekly and per-model weekly windows run seven days, and those
# are exactly the ones that bind for days at a time. It also never once fired,
# because the hook was then registered at `timeout: 21600` -- the identical
# number -- and the harness clock starts ~0.2s earlier at spawn.
#
# Removing it changed nothing for the same reason, which is what made the leak
# above so hard to see: the ceiling everyone was looking for had never been the
# one in this file. HOOK_TIMEOUT is the real ceiling; `max_delay` is the one you
# are meant to set.
#
# blind_delay (above) is a ceiling too, but only on a hold that cannot see. A
# hold justified by data still has none of its own.

# How far past a printed reset time a reading may still be describing a LIVE
# window. The server rounds the times it renders -- 8:09pm and 8:10pm are the
# same instant -- so a window in its final minute can read as already past its
# own reset while it is still running. Beyond this slack it has genuinely
# rolled.
#
# One copy because the CAUSE is one -- the rounding above -- not because the
# three readers are coupled. They are not, and the difference matters to
# anyone who moves the number. bucket_pace uses it as a ONE-SIDED slack past a
# single printed reset: how late a reading may be and still describe a live
# window. plot._segments and cli.cmd_check use it as a SYMMETRIC identity
# tolerance on the gap between two consecutive reset clauses
# (`abs(reset - prev_reset) > RESET_JITTER`): are these two samples talking
# about the same window?
#
# So raising it to absorb coarser server rounding costs something in both of
# those, by one mechanism: a real roll whose clause moved by less than the new
# threshold stops counting as a roll. In _segments the trace is then drawn
# straight across it, implying a fall in usage that never happened. In
# cmd_check `rolled` is what SUPPRESSES the usage-decreased warning, so that
# roll's legitimate drop gets reported as a likely misparse -- the check fires
# MORE, not less. Read all three before changing it.
#
# It had been two independent copies, in cli and in plot, which is the same
# disease bucket_pace itself was centralised to cure.
RESET_JITTER = 120

WINDOW_SECONDS = {"session": 5 * 3600, "week": 7 * 86400}

DEFAULT_POLICY = {
    "global": {"enabled": True},
    "defaults": {"m0": DEFAULT_M0, "m1": DEFAULT_M1, "chunk": DEFAULT_CHUNK,
                 "max_delay": DEFAULT_MAX_DELAY, "band": DEFAULT_BAND,
                 "band_delay": DEFAULT_BAND_DELAY,
                 "blind_delay": DEFAULT_BLIND_DELAY},
    "paths": {},
}


# Which windows a folder is paced against. Not every project wants both: work
# you are actively tending wants the 5-hour line to smooth it out, but has no
# reason to answer to the weekly line, which exists to protect budget for days
# you are not here.
#   session -- the 5h window
#   week    -- the shared weekly window (`week:all models`)
#   model   -- the per-model weekly window, if the declared model has one
DEFAULT_ENFORCE = ("session", "week", "model")
VALID_ENFORCE = frozenset(DEFAULT_ENFORCE)


def normalize_enforce(value):
    """Coerce a configured `enforce` value to a set of known window names.

    Falls back to enforcing everything when the value is missing, empty, or
    contains nothing recognizable. That direction is deliberate: this tool
    exists to restrain spending, so a malformed config must not silently
    un-pace a folder that looks paced.
    """
    if value is None:
        return set(DEFAULT_ENFORCE)
    if isinstance(value, str):
        value = [v.strip() for v in value.split(",")]
    try:
        chosen = {str(v).strip().lower() for v in value} & VALID_ENFORCE
    except TypeError:
        return set(DEFAULT_ENFORCE)
    return chosen or set(DEFAULT_ENFORCE)


def coerce_num(value, fallback):
    """Coerce a policy number, falling back rather than raising.

    policy.json is hand-edited, and a quoted number -- `"band": "2"` -- is the
    obvious slip. The pace arithmetic raises TypeError on a str, and the hook
    fails open by design, so one quotation mark would un-pace the folder on
    EVERY tool call while `status` went on calling it paced. That is the exact
    outcome this tool exists to prevent, and it is the same direction
    normalize_enforce already falls in: an unusable value is not a licence to
    spend.

    NaN is rejected too, and not pedantically. Every comparison against NaN is
    False, so a NaN margin reads as "never over the line" -- it would not crash,
    it would quietly stop braking, which is worse.
    """
    if value is None:
        return fallback
    try:
        n = float(value)
    except (TypeError, ValueError):
        return fallback
    if n != n or n in (float("inf"), float("-inf")):
        return fallback
    return n


def off_or_num(entry, defaults, key, fallback):
    """Resolve a knob whose `null` means OFF rather than "not set".

    `max_delay`, `band`, `band_delay` and `blind_delay` are all switches with
    a number on them, and the CLI turns one off by writing an explicit `null`
    into the rule (`--no-max-delay`, `--no-band-delay`, `--no-blind-delay`)
    rather than by deleting the key
    -- deleting it would inherit whatever `defaults` says, which is the
    opposite of what the flag asked for.

    That distinction only became load-bearing when the shipped defaults for
    `band` and `band_delay` stopped being "off": with coerce_num alone, a
    written `null` fell back to the built-in and the off switch quietly turned
    the knob back on. So the key's PRESENCE picks the source, and a present
    null is returned as None. Garbage still falls back, as everywhere else: an
    unusable value is not a licence to spend.
    """
    for src in (entry, defaults):
        if key in src:
            value = src[key]
            return None if value is None else coerce_num(value, fallback)
    return fallback


def blind_delay_of(entry, defaults):
    """The `blind_delay` in force for a rule, in seconds, or None for off.

    One resolver for the hook and `status` both, so the line `status` prints
    cannot disagree with the hold the hook takes. Read with off_or_num, since
    it ships set and a written null has to mean off. A negative value clamps
    to 0 -- "never hold blind" -- rather than None: unlike band_delay, whose
    0 means no throttle, the closest honest reading of "less than no wait" is
    no wait, not an unbounded one.
    """
    blind_delay = off_or_num(entry, defaults, "blind_delay",
                             DEFAULT_BLIND_DELAY)
    if blind_delay is not None and blind_delay < 0:
        blind_delay = 0
    return blind_delay


def model_matches(bucket_key, model):
    """Does `bucket_key` name the per-model weekly bucket for `model`?

    Exact comparison is wrong. The renderer produces these labels two different
    ways: a hardcoded "Current week (Sonnet only)" for max/team subscriptions,
    and a server-supplied `displayName` for model-scoped limits (which is where
    "(Fable)" comes from). So the label is neither stable nor predictable, and
    `week:sonnet` never equals `week:sonnet only`.

    Match on whole words instead, which handles both forms and any future
    display name that contains the model's name.
    """
    if not model:
        return False
    key = bucket_key.lower()
    if not key.startswith("week:"):
        return False
    label = key[len("week:"):]
    if label == "all models":       # the shared bucket, never model-scoped
        return False
    words = label.replace("(", " ").replace(")", " ").replace("-", " ").split()
    return model.lower() in words


def norm_path(p):
    """Canonical form for policy-key comparison.

    normcase matters on Windows, where paths are case-insensitive and use
    backslashes; without it, C:\\Proj and c:\\proj would be different keys and a
    folder could look unpaced when it isn't. normpath strips trailing
    separators without mangling a bare root or a drive root.
    """
    return os.path.normcase(os.path.normpath(os.path.realpath(os.path.expanduser(p))))


def path_within(cwd, key):
    """Is `cwd` at or below policy key `key`? Both must already be norm_path'd.

    Component-wise, not raw string prefix: /foo/bar must not match a rule on
    /foo/ba, which would silently pace the wrong tree.

    A root key is the one normalized path that already ends in a separator
    ("/" on POSIX, "C:\\" on Windows -- normpath preserves those). Appending
    another separator unconditionally would build "//" and match nothing
    beneath it, which is what used to make a rule on the root apply to the root
    directory alone. Both resolvers share this helper so `status` cannot
    disagree with the hook about which rule governs a folder.
    """
    if cwd == key:
        return True
    prefix = key if key.endswith(os.sep) else key + os.sep
    return cwd.startswith(prefix)


def bucket_pace(bucket, now, m0, m1, band=0):
    """Where one usage bucket stands against the pace lines, and when it clears.

    The hook and `status` both need this arithmetic, and a second copy of it is
    how they would begin to disagree -- the same reason `path_within` is shared.

    Returns None for a bucket carrying no usable percentage -- missing, or a
    value arithmetic cannot use (see the first guard below). Otherwise a dict:

        pct      as reported by /usage
        pess     pct + 1. /usage reports whole percents, so a reported P could
                 really be up to P+1; round against ourselves.
        allowed  the brake line's height right now, in percent
        low      the throttle line's height right now, `band` below the brake
                 line but never below m0 (see below)
        elapsed  fraction of the window elapsed, never above 1.0, or None
                 when there is no window in progress to measure: the snapshot
                 carries no reset clause -- which is what a freshly rolled
                 window looks like -- or the window has expired. allowed()
                 never dips below m0, so m0 is the safe floor to judge against
                 until the clause reappears.
        expired  has the window rolled since this reading was taken? True
                 voids the reading entirely; see the branch below for what the
                 rest of the shape then means, which is NOT a position.
        region   'free' under the throttle line, 'band' between the lines,
                 'over' above the brake line
        over     is `pess` above the BRAKE line, i.e. region == 'over'. Kept
                 under its old name because it still means what it always did.
        wait     seconds until the THROTTLE line rises to meet `pess`, or None
                 while free or when the window start is unknown. Note the
                 target: both braking regions aim at the same place, and the
                 brake line only decides whether the hold is capped.
        wake     the epoch that wait counts down to, clamped to the window's own
                 reset, which zeroes usage anyway

    Every one of those keys is present on every shape this returns, expired
    or not, so no consumer can be surprised by a missing one. See the expired
    branch for why that totality is load-bearing rather than merely tidy.

    The throttle line is floored at m0 rather than allowed to sink below it.
    m0 exists so that a fresh window is startable at all -- the pure diagonal
    permits 0% at 0% elapsed -- and throttling the first step out of a window
    that has just rolled would be perverse. So the band opens as the window
    advances, reaching its full width once the brake line has climbed `band`
    above m0, and before that the grubstake is free at full speed.
    """
    pct = bucket.get("pct")
    if not isinstance(pct, (int, float)):
        # None is the documented "no percentage" case. The rest of the test
        # is the same fail-open guard as `placed` below: pct is read back out
        # of state.json unvalidated, and a quoted "50" raised TypeError at
        # `pess = pct + 1` -- which hook.main catches, logs `-- failing
        # open`, and turns into a folder that is silently not paced at all.
        # A pct that arithmetic cannot use IS a bucket carrying no
        # percentage, so it takes the contract's existing None return, which
        # hook.decide already files as an unusable -- and therefore blind --
        # hot entry. No new policy. A JSON `true` still slips through here,
        # because Python's bool is an int; that is sighting 15 in
        # harness/open-questions.md, still open, and deliberately not
        # resolved in this guard.
        return None
    span = 100 - m0 - m1
    if span <= 0:
        span = 1  # a nonsensical config must not divide by zero
    band = max(0, band or 0)
    pess = pct + 1
    resets = bucket.get("resets_epoch")
    window = bucket.get("window_seconds")
    # `placed` must be decided before ANY arithmetic touches these two -- the
    # `resets - window` subtraction included, which is why there is a flag
    # here rather than a bare ternary on None. An earlier version tested only
    # `window <= 0`, AFTER that subtraction, so a non-numeric value in either
    # field sailed straight past the guard whose comment claimed to close
    # this hole: a quoted "18000" raised TypeError at the subtraction itself.
    placed = (isinstance(resets, (int, float))
              and isinstance(window, (int, float)) and window > 0)
    start = resets - window if placed else None
    if start is None or now < start:
        # Several ways to have no window position at all, answered by one
        # return because they deserve one answer. f_t is unknown in each, so
        # both lines collapse onto the m0 floor. There is no band to be in:
        # either the grubstake covers you or you are over, and the latter
        # cannot be solved for a wake time.
        #
        # `placed` is a fail-open guard rather than pedantry. Both clause
        # fields are read back out of state.json unvalidated, and each kind
        # of garbage found its own way in. A window of 0 divides by zero; a
        # non-numeric value in either field raises TypeError before it even
        # gets that far. Both raises land in hook.main's catch-all, which
        # logs `-- failing open` and turns one corrupt field into a folder
        # that is silently not paced at all -- the exact outcome bucket_pace's
        # total return shape exists to prevent. A negative window is worse
        # than either, because it does not raise: it puts `start` AFTER
        # `resets`, so the pre-start test below cannot catch it, and -5
        # measured out at elapsed -9.0 and a brake line of -778%, held `over`
        # with a wait of zero.
        #
        # Why the floor shape and not the void one, for all of that garbage:
        # garbage in the window POSITION does not make the pct garbage. The
        # pct is an independent and typically fresh measurement; only the
        # position is destroyed, and the m0 floor is already this codebase's
        # answer to "fresh pct, no usable position". It still brakes whenever
        # `pess > m0`, so nothing is allowed that a missing clause would not
        # also allow. The void shape would assert a specific falsehood --
        # that the window has rolled, when its length is unreadable -- and
        # its `expired: True` forces a refresh in `run` on the first pass of
        # every invocation, so a daemon bug writing a persistently bad
        # window_seconds would cost a ~2s subprocess plus a blind hold per
        # tool call forever, even at 3% usage.
        #
        # The two honest cases. No reset clause at all is exactly what a
        # window that has just rolled looks like, and the state `run`
        # refreshes INTO when it finds an expired one.
        #
        # The other is a clause so far out that the window it implies has not
        # begun: `now` before `resets - window`. Clock skew does it, and so
        # does a reparse landing a session reset hours ahead. It used to
        # fall through to the solve below, where it was wrong twice over.
        # Measured on a 5h window with the clause 20h out, pct 50: the lines
        # ran negative -- elapsed -3.0, a brake line of -256%, which nothing
        # can ever be under -- and, far worse, the solve returned a wake
        # 18.05h away that `decide` read as REAL. That made the hold
        # CONFIDENT rather than blind, and blind_delay cannot release a
        # confident hold while max_delay is off by default, so the only
        # ceiling left was the 48h hook timeout: a stall measured in days,
        # against a user rule that says fail free rather than stall forever.
        # Returning `wake: None` here is what hands it to THE BLIND RULE
        # instead, which bounds it at blind_delay.
        #
        # Clamping the lines alone would NOT have been enough, and would have
        # been worse than leaving them: the row then reads `over line 5.0%` at
        # `0.0% elapsed`, indistinguishable from an ordinary start-of-window
        # brake, where before it read `over line -256.0%` and was obviously
        # broken. The whole shape has to be the floor shape, not just the two
        # numbers in it.
        region = "over" if pess > m0 else "free"
        return {"pct": pct, "pess": pess, "allowed": m0, "low": m0,
                "elapsed": None, "region": region, "over": region == "over",
                "wait": None, "wake": None, "expired": False}
    if now > resets + RESET_JITTER:
        # The window this reading describes has rolled, so the reading is VOID
        # -- it is not a position. It is not a lower bound on current usage,
        # because the window was zeroed; and it is not evidence of headroom,
        # because the window now running has never been observed. Treating it
        # as a position and extrapolating off the end of its window is what
        # put the brake line at 853.9% and `elapsed` at 1055.5% on a session
        # snapshot read 52h late, and what then let that void reading classify
        # `free`.
        #
        # Clamping `elapsed` to 1.0 instead would be a regression, not a
        # lesser fix: a stale 95% session reading would then sit permanently
        # over the brake line's final height of 92% (`100 - m1` at the
        # shipped margins) and brake on a window that reset days ago. The
        # extrapolation is currently the only thing preventing that, so the
        # reading has to be voided rather than re-anchored.
        #
        # Strictly greater, and against the jitter slack rather than `resets`
        # itself. The slack is not padding: the server rounds the reset times
        # it prints, so a live window in its final minute reads as already
        # past its own reset, and without the slack EVERY window end would
        # spend two minutes voiding readings that are perfectly good.
        #
        # What that buys is the `free` and `band` cases, which is where it
        # pays. An under-the-line reading in those two minutes stays under the
        # line instead of becoming a blind hard hold, and a banded one keeps
        # its lower gear. It buys nothing for an over-the-line reading, and
        # that is worth knowing rather than claiming otherwise: inside the
        # zone such a reading solves its wake to the reset just behind us, so
        # hook.decide's `real` guard refuses it and THE BLIND RULE marks the
        # hold blind -- the same answer expiry would have given.
        #
        # The strict `>` decides exactly one instant, `resets + RESET_JITTER`
        # itself, which stays live. It is NOT what makes plot._unit_pace
        # immune: that asks about `now == resets` on a one-second window, and
        # 1.0 is below 1.0 + 120 under either operator. The slack alone
        # carries that case, as _unit_pace's own docstring says.
        #
        # The shape is TOTAL: every key a live shape carries, with numbers
        # wherever a consumer will format numbers. hook.main catches every
        # exception and fails open, so a partial shape that crashed some
        # forgotten consumer would not void one reading -- it would stop
        # pacing the folder altogether, which is worse than either braking or
        # running free. It also fails toward HOLDING: region "over" and `over`
        # True, so a consumer that has never heard of `expired` stops rather
        # than proceeds, with `wait` and `wake` None because there is no line
        # left to solve against. `allowed` and `low` sit on the m0 floor
        # purely so a forgetful formatter cannot raise -- they are a crash
        # guard, NOT a line that was ever in force, and no consumer may
        # present them as one.
        #
        # In hook.decide this lands as a wake-less hot entry, which THE BLIND
        # RULE already classifies blind. That is no new policy: "cannot see"
        # is what this codebase already answers an empty snapshot and a
        # degraded under-the-line reading with, and a void reading is strictly
        # less informative than a degraded one.
        return {"pct": pct, "pess": pess, "allowed": m0, "low": m0,
                "elapsed": None, "region": "over", "over": True,
                "wait": None, "wake": None, "expired": True}
    # Clamped above, never extrapolated: past the printed reset but inside
    # the slack, "the line is at its final height" is true for that last
    # minute; "the line is at 103%" never is. With the expiry test above it is
    # what retires both the 1055.5% elapsed figure `status` printed and the
    # band -> free slide that let a just-rolled window take a soft hold
    # against a wake already behind it.
    #
    # The clamp is one-sided on purpose. Given the branch above -- which has
    # returned for every non-positive window and for every `now` below `start`
    # -- the quotient here is at least 0, so a max(0.0, ...) would be
    # unreachable. Note that only the LOW side is settled: the quotient does
    # exceed 1 in the jitter zone (1.006667 at `now == resets + RESET_JITTER`
    # on a 5h window), which is what the min above is for.
    #
    # The max is also left out because writing it back would read as the fix
    # for the negative case, which it is not: the damage there was the wake
    # solve returning a far-future instant that `decide` trusted, not the
    # lines, and clamping the lines alone would have hidden it.
    elapsed = min(1.0, (now - start) / window)
    allowed = m0 + elapsed * span
    low = max(m0, allowed - band)
    if pess <= low:
        return {"pct": pct, "pess": pess, "allowed": allowed, "low": low,
                "elapsed": elapsed, "region": "free", "over": False,
                "wait": None, "wake": None, "expired": False}
    region = "over" if pess > allowed else "band"
    # Solve for the throttle line, not the brake line: `low` is the release
    # target in both braking regions. Not free implies pess > low >= m0, so the
    # m0 floor can never be the branch that satisfies us and the solve is
    # unconditional.
    wake = min(start + ((pess + band - m0) / span) * window, resets)
    return {"pct": pct, "pess": pess, "allowed": allowed, "low": low,
            "elapsed": elapsed, "region": region, "over": region == "over",
            "wait": max(0.0, wake - now), "wake": wake, "expired": False}
