"""niceclaude hook -- tier 1, the hot path.

Runs on every PreToolUse and SubagentStart, in the main agent AND inside every
subagent, so it stays stdlib-only and imports nothing heavy. `subprocess` is
imported lazily, on the rare path where the snapshot has gone stale.

It holds no policy of its own. The daemon publishes a raw usage snapshot to
state.json; this resolves the folder policy for its own cwd and does the
pace-line arithmetic itself. One snapshot therefore serves many folders under
different policies.

Braking is a SLEEP, not a denial. Returning non-zero hands the model a refusal
to reason about, which costs tokens and derails the task. Sleeping freezes the
agent in place holding its context -- and PreToolUse fires between API turns,
so nothing is in flight and there is no connection to rot.

Claude Code invokes this synchronously and blocks on it. That blocking IS the
freeze. This module must therefore never fork or background itself: if it
returned early the agent would sail straight through and the pacer would look
installed while doing nothing.
"""

import json
import os
import sys
import time

from ._shared import (
    DEFAULT_CHUNK, DEFAULT_FANOUT_RESERVE, DEFAULT_M0, DEFAULT_M1,
    HOOK_LOG_PATH, MAX_BRAKE,
    MAX_STALE, POLICY_PATH, STATE_PATH, model_matches, norm_path,
)


def load_json(path, fallback):
    try:
        with open(path, encoding="utf-8") as fh:
            return json.load(fh)
    except (OSError, ValueError):
        return fallback


def log(msg):
    try:
        with open(HOOK_LOG_PATH, "a", encoding="utf-8") as fh:
            fh.write(f"{time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime())} {msg}\n")
    except OSError:
        pass


def resolve(policy, cwd):
    """Longest matching path prefix wins, so subfolders inherit and a deeper
    rule can override a shallower one.

    Compares whole path components: a raw string prefix would let /foo/bar
    match /foo/barbaz and silently pace the wrong tree.
    """
    best = None
    for raw, entry in (policy.get("paths") or {}).items():
        try:
            key = norm_path(raw)
        except (OSError, ValueError):
            continue
        if cwd == key or cwd.startswith(key + os.sep):
            if best is None or len(key) > len(best[0]):
                best = (key, entry)
    return best


def paced_entry(policy, cwd):
    """The policy entry governing `cwd`, or None if it is not paced at all.

    Split out so the hot path can answer "is this folder even paced?" from
    policy.json alone -- no snapshot, no network, no subprocess. Getting that
    order wrong made unpaced folders pay a ~2s `claude -p /usage` refresh on
    every tool call whenever the snapshot was stale, which is the normal state
    of affairs if the hook is installed but no daemon is running.
    """
    if not (policy.get("global") or {}).get("enabled", True):
        return None
    match = resolve(policy, cwd)
    if match is None or not match[1].get("paced", False):
        return None
    return match[1]


def decide(policy, state, cwd, now, degraded=False, event=None):
    """Return {'paced':..,'braked':..,'wake_at':..,'reason':..,'blind':..}.

    `degraded` means the snapshot is older than MAX_STALE and could not be
    refreshed -- we are flying blind. Stale data is not useless, though: usage
    only ever rises within a window, so an old reading is a LOWER bound on
    current consumption. That asymmetry is the whole policy here --

        stale data can justify BRAKING, but never ALLOWING.

    So a degraded snapshot that already says "over the line" is acted on with
    full confidence (it can only have got worse), while a degraded snapshot that
    says "under the line" is treated as unknown and brakes anyway.

    Note there is no truthiness trap here: the jq version's `//` operator falls
    back on false as well as null, which silently disabled the kill switch and
    would have turned a configured m0 of 0 into 5. dict.get with a default only
    fires on a missing key.
    """
    entry = paced_entry(policy, cwd)
    if entry is None:
        return {"paced": False}

    defaults = policy.get("defaults") or {}
    m0 = entry.get("m0", defaults.get("m0", DEFAULT_M0))
    m1 = entry.get("m1", defaults.get("m1", DEFAULT_M1))
    # A fan-out is a much larger commitment than one more step of work already
    # in flight, so SubagentStart may be held to a stricter line than
    # PreToolUse. Raising the reserve raises the bar without touching the
    # control law itself.
    if event == "SubagentStart":
        m1 += entry.get("fanout_reserve",
                        defaults.get("fanout_reserve", DEFAULT_FANOUT_RESERVE))
    chunk = entry.get("chunk", defaults.get("chunk", DEFAULT_CHUNK))
    model = (entry.get("model") or "").lower()
    span = 100 - m0 - m1
    if span <= 0:
        span = 1  # a nonsensical config must not divide by zero

    buckets = state.get("buckets") or {}
    enforced = [
        (k, b) for k, b in buckets.items()
        if k in ("session", "week:all models") or model_matches(k, model)
    ]
    if not enforced:
        return {"paced": True, "braked": True, "wake_at": now + chunk,
                "reason": "no usable buckets in snapshot", "chunk": chunk,
                "blind": True}

    hot = []
    for key, b in enforced:
        pct = b.get("pct")
        if pct is None:
            hot.append((key, f"{key}: unusable", now + chunk))
            continue
        # /usage reports whole percents, so a reported P could really be up to
        # P+1. Round against ourselves.
        pess = pct + 1
        resets = b.get("resets_epoch")
        window = b.get("window_seconds")
        if resets is None or window is None:
            # Right after a window rolls the server omits the reset clause, so
            # f_t is unknown. allowed() never dips below m0, so judge against m0
            # -- a freshly rolled 0%-used window sails through instead of
            # braking at the exact moment headroom is greatest.
            if pess > m0:
                hot.append((key, f"{key} {pct}% over floor {m0}%", now + chunk))
            continue
        start = resets - window
        ft = (now - start) / window
        allowed = m0 + ft * span
        if pess > allowed:
            # Wake when the line rises to meet us -- but never later than the
            # window's own reset, which zeroes usage anyway.
            wake = min(start + ((pess - m0) / span) * window, resets)
            hot.append((key, f"{key} {pct}% over line {allowed:.1f}%", wake))

    if hot:
        # Confident even when degraded: consumption only rises, so a stale
        # reading that is already over the line is a floor, not a guess.
        return {"paced": True, "braked": True,
                "wake_at": max(h[2] for h in hot),
                "reason": "; ".join(h[1] for h in hot),
                "chunk": chunk, "blind": False}

    if degraded:
        # Under the line according to data we know to be out of date. That is
        # not evidence of headroom, so hold rather than guess. Logged
        # distinctly: "braked because blind" and "braked because hot" are
        # completely different problems at 3am.
        age = int(now - (state.get("ts_epoch") or now))
        return {"paced": True, "braked": True, "wake_at": now + chunk,
                "reason": f"BLIND: snapshot {age}s old and refresh failing",
                "chunk": chunk, "blind": True}

    return {"paced": True, "braked": False, "chunk": chunk, "blind": False}


# Backoff for refresh attempts. If /usage is unreachable -- network down, auth
# expired, or the service itself unhappy -- retrying every few seconds neither
# helps nor is polite. Escalate, then hold at five minutes.
REFRESH_BACKOFF = (0, 15, 30, 60, 120, 300)


def refresh_snapshot():
    """Self-heal when the daemon is down or lagging. Returns True on success.

    Costs a couple of seconds on this one tool call, but stops a dead daemon
    from either wedging the agent or silently letting it run unpaced.
    subprocess is imported here so the common path never pays for it.
    """
    import subprocess
    exe = os.environ.get("NICECLAUDE_BIN") or "niceclaude"
    try:
        proc = subprocess.run([exe, "refresh"], capture_output=True, timeout=180)
        return proc.returncode == 0
    except Exception:
        return False


def snapshot_age(state, now):
    """Age from the snapshot's own timestamp rather than the file's mtime: a
    rewritten-but-failed refresh must not look fresh."""
    ts = state.get("ts_epoch")
    if not isinstance(ts, (int, float)):
        return None
    return now - ts


def run(cwd, event=None):
    brake_start = None
    fails = 0            # consecutive failed refreshes, indexes REFRESH_BACKOFF
    next_try = 0.0       # earliest time we may attempt another refresh
    was_blind = False

    while True:
        now = time.time()
        policy = load_json(POLICY_PATH, {})

        # Cheapest possible gate, and it must come first: an unpaced folder
        # needs no usage data, so it must never trigger a refresh. This is the
        # common case -- every foreground session, on every tool call.
        if paced_entry(policy, cwd) is None:
            return brake_start, "unpaced"

        state = load_json(STATE_PATH, {})
        age = snapshot_age(state, now)

        if (age is None or age > MAX_STALE) and now >= next_try:
            if refresh_snapshot():
                fails = 0
                next_try = 0.0
                state = load_json(STATE_PATH, {})
                age = snapshot_age(state, now)
            else:
                fails = min(fails + 1, len(REFRESH_BACKOFF) - 1)
                next_try = now + REFRESH_BACKOFF[fails]
            now = time.time()

        degraded = age is None or age > MAX_STALE
        d = decide(policy, state, cwd, now, degraded=degraded, event=event)

        if not d.get("paced"):
            # Kill switch or policy change, possibly mid-brake.
            return brake_start, "unpaced"
        if not d.get("braked"):
            return brake_start, "line-caught-up"

        if brake_start is None:
            brake_start = now
            was_blind = d.get("blind", False)
            log(f"brake  cwd={cwd} [{event or 'PreToolUse'}] {d.get('reason', '')}")
        elif d.get("blind", False) != was_blind:
            # Crossing between "over the line" and "cannot see" mid-brake is a
            # material change in why we are stopped; record it.
            was_blind = d.get("blind", False)
            log(f"brake* cwd={cwd} {d.get('reason', '')}")

        # Give up after MAX_BRAKE. By then every window has rolled, so still
        # holding would mean something is wrong with us, not with the budget.
        if now - brake_start >= MAX_BRAKE:
            return brake_start, ("MAX_BRAKE-timeout-WHILE-BLIND"
                                 if d.get("blind") else "MAX_BRAKE-timeout")

        # Sleep in bounded chunks and re-decide. The wake time is NOT a
        # commitment: the foreground session and sibling agents draw on the same
        # account-global budget and can push it later while we wait.
        #
        # We keep waking on `chunk` even while backing off from a failed
        # refresh. The refresh itself stays rate-limited by next_try, but policy
        # is re-read every cycle, so the kill switch still frees a blind agent
        # within one chunk instead of one backoff interval.
        chunk = d.get("chunk", DEFAULT_CHUNK)
        nap = min(chunk, max(1.0, d.get("wake_at", now + chunk) - now))
        time.sleep(nap)


def main():
    try:
        raw = sys.stdin.read()
    except Exception:
        return 0
    try:
        payload = json.loads(raw) if raw.strip() else {}
    except ValueError:
        return 0

    cwd = payload.get("cwd")
    if not cwd:
        return 0
    if not os.path.exists(POLICY_PATH):
        return 0
    try:
        cwd = norm_path(cwd)
    except (OSError, ValueError):
        return 0

    try:
        brake_start, why = run(cwd, payload.get('hook_event_name'))
    except Exception as exc:
        # Fail open. A bug in here must never wedge every session; the daemon
        # and `niceclaude check` are where problems should surface.
        log(f"ERROR cwd={cwd} {type(exc).__name__}: {exc} -- failing open")
        return 0

    # Every exit from a brake is logged, whatever ended it. hook.log is the only
    # record of what the pacer did overnight, and a brake with no matching
    # release reads as a hang.
    if brake_start is not None:
        log(f"release cwd={cwd} after {int(time.time() - brake_start)}s ({why})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
