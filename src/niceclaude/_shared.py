"""Paths and constants shared by the hook and the CLI.

Imports `os` and nothing else. The hook is on the hot path for every tool call
in every agent and subagent, so anything it imports transitively is a tax paid
thousands of times a night. Measured: stdlib-only hook is 16ms, the same module
plus argparse/re/subprocess is 29ms.
"""

import os

HOME = os.path.expanduser("~")


def _data_dir():
    override = os.environ.get("NICECLAUDE_DIR")
    if override:
        return override
    if os.name == "nt":
        base = os.environ.get("LOCALAPPDATA") or os.path.join(HOME, "AppData", "Local")
        return os.path.join(base, "niceclaude")
    return os.path.join(HOME, ".local", "share", "niceclaude")


def _config_dir():
    override = os.environ.get("NICECLAUDE_CONFIG_DIR")
    if override:
        return override
    # If the data dir has been relocated, keep config alongside it. Otherwise a
    # container would need two bind mounts to persist state, and the second one
    # is easy to forget -- losing settings.json silently unpaces everything.
    data_override = os.environ.get("NICECLAUDE_DIR")
    if data_override:
        return os.path.join(data_override, "config")
    if os.name == "nt":
        base = os.environ.get("APPDATA") or os.path.join(HOME, "AppData", "Roaming")
        return os.path.join(base, "niceclaude")
    return os.path.join(HOME, ".config", "niceclaude")


DATA_DIR = _data_dir()
CONFIG_DIR = _config_dir()

LOG_PATH = os.path.join(DATA_DIR, "usage.jsonl")
STATE_PATH = os.path.join(DATA_DIR, "state.json")
POLICY_PATH = os.path.join(DATA_DIR, "policy.json")
HOOK_LOG_PATH = os.path.join(DATA_DIR, "hook.log")
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

WINDOW_SECONDS = {"session": 5 * 3600, "week": 7 * 86400}

DEFAULT_POLICY = {
    "global": {"enabled": True},
    "defaults": {"m0": DEFAULT_M0, "m1": DEFAULT_M1, "chunk": DEFAULT_CHUNK,
                 "max_delay": DEFAULT_MAX_DELAY, "band": DEFAULT_BAND,
                 "band_delay": DEFAULT_BAND_DELAY},
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

    `max_delay`, `band` and `band_delay` are all switches with a number on
    them, and the CLI turns one off by writing an explicit `null` into the
    rule (`--no-max-delay`, `--no-band-delay`) rather than by deleting the key
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

    Returns None for a bucket carrying no percentage. Otherwise a dict:

        pct      as reported by /usage
        pess     pct + 1. /usage reports whole percents, so a reported P could
                 really be up to P+1; round against ourselves.
        allowed  the brake line's height right now, in percent
        low      the throttle line's height right now, `band` below the brake
                 line but never below m0 (see below)
        elapsed  fraction of the window elapsed, or None when the snapshot
                 carries no reset clause -- which is what a freshly rolled
                 window looks like. allowed() never dips below m0, so m0 is the
                 safe floor to judge against until the clause reappears.
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

    The throttle line is floored at m0 rather than allowed to sink below it.
    m0 exists so that a fresh window is startable at all -- the pure diagonal
    permits 0% at 0% elapsed -- and throttling the first step out of a window
    that has just rolled would be perverse. So the band opens as the window
    advances, reaching its full width once the brake line has climbed `band`
    above m0, and before that the grubstake is free at full speed.
    """
    pct = bucket.get("pct")
    if pct is None:
        return None
    span = 100 - m0 - m1
    if span <= 0:
        span = 1  # a nonsensical config must not divide by zero
    band = max(0, band or 0)
    pess = pct + 1
    resets = bucket.get("resets_epoch")
    window = bucket.get("window_seconds")
    if resets is None or window is None:
        # No reset clause, so f_t is unknown and both lines collapse onto the m0
        # floor. There is no band to be in: either the grubstake covers you or
        # you are over, and the latter cannot be solved for a wake time.
        region = "over" if pess > m0 else "free"
        return {"pct": pct, "pess": pess, "allowed": m0, "low": m0,
                "elapsed": None, "region": region, "over": region == "over",
                "wait": None, "wake": None}
    start = resets - window
    elapsed = (now - start) / window
    allowed = m0 + elapsed * span
    low = max(m0, allowed - band)
    if pess <= low:
        return {"pct": pct, "pess": pess, "allowed": allowed, "low": low,
                "elapsed": elapsed, "region": "free", "over": False,
                "wait": None, "wake": None}
    region = "over" if pess > allowed else "band"
    # Solve for the throttle line, not the brake line: `low` is the release
    # target in both braking regions. Not free implies pess > low >= m0, so the
    # m0 floor can never be the branch that satisfies us and the solve is
    # unconditional.
    wake = min(start + ((pess + band - m0) / span) * window, resets)
    return {"pct": pct, "pess": pess, "allowed": allowed, "low": low,
            "elapsed": elapsed, "region": region, "over": region == "over",
            "wait": max(0.0, wake - now), "wake": wake}
