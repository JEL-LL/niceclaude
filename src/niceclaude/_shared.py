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

MAX_STALE = 180      # a snapshot older than this is not trusted
MAX_BRAKE = 21600    # 6h; by then every window has certainly rolled

WINDOW_SECONDS = {"session": 5 * 3600, "week": 7 * 86400}

DEFAULT_POLICY = {
    "global": {"enabled": True},
    "defaults": {"m0": DEFAULT_M0, "m1": DEFAULT_M1, "chunk": DEFAULT_CHUNK,
                 "max_delay": DEFAULT_MAX_DELAY},
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


def bucket_pace(bucket, now, m0, m1):
    """Where one usage bucket stands against the pace line, and when it clears.

    The hook and `status` both need this arithmetic, and a second copy of it is
    how they would begin to disagree -- the same reason `path_within` is shared.

    Returns None for a bucket carrying no percentage. Otherwise a dict:

        pct      as reported by /usage
        pess     pct + 1. /usage reports whole percents, so a reported P could
                 really be up to P+1; round against ourselves.
        allowed  the line's height right now, in percent
        elapsed  fraction of the window elapsed, or None when the snapshot
                 carries no reset clause -- which is what a freshly rolled
                 window looks like. allowed() never dips below m0, so m0 is the
                 safe floor to judge against until the clause reappears.
        over     is `pess` above the line
        wait     seconds until the line rises to meet `pess`, or None when
                 nothing is over the line or the window start is unknown
        wake     the epoch that wait counts down to, clamped to the window's own
                 reset, which zeroes usage anyway
    """
    pct = bucket.get("pct")
    if pct is None:
        return None
    span = 100 - m0 - m1
    if span <= 0:
        span = 1  # a nonsensical config must not divide by zero
    pess = pct + 1
    resets = bucket.get("resets_epoch")
    window = bucket.get("window_seconds")
    if resets is None or window is None:
        return {"pct": pct, "pess": pess, "allowed": m0, "elapsed": None,
                "over": pess > m0, "wait": None, "wake": None}
    start = resets - window
    elapsed = (now - start) / window
    allowed = m0 + elapsed * span
    if pess <= allowed:
        return {"pct": pct, "pess": pess, "allowed": allowed,
                "elapsed": elapsed, "over": False, "wait": None, "wake": None}
    wake = min(start + ((pess - m0) / span) * window, resets)
    return {"pct": pct, "pess": pess, "allowed": allowed, "elapsed": elapsed,
            "over": True, "wait": max(0.0, wake - now), "wake": wake}
