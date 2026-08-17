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

MAX_STALE = 180      # a snapshot older than this is not trusted
MAX_BRAKE = 21600    # 6h; by then every window has certainly rolled

WINDOW_SECONDS = {"session": 5 * 3600, "week": 7 * 86400}

DEFAULT_POLICY = {
    "global": {"enabled": True},
    "defaults": {"m0": DEFAULT_M0, "m1": DEFAULT_M1, "chunk": DEFAULT_CHUNK},
    "paths": {},
}


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
