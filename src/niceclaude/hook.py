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
    ACCOUNT_KEY,
    DEFAULT_BAND, DEFAULT_BAND_DELAY, DEFAULT_CHUNK, DEFAULT_FANOUT_RESERVE,
    DEFAULT_M0, DEFAULT_M1, DEFAULT_MAX_DELAY, HOOK_LOG_PATH,
    blind_delay_of, bucket_pace, coerce_num, normalize_enforce, MAX_STALE,
    NEAR_STALE, off_or_num,
    POLICY_PATH,
    STATE_PATH, model_matches, norm_path, path_within,
)


def load_json(path, fallback):
    try:
        with open(path, encoding="utf-8") as fh:
            return json.load(fh)
    except (OSError, ValueError):
        return fallback


# What load_state reports for a snapshot that parsed but carries no stamp at
# all, under a non-default key. Angle-bracketed so it can never be mistaken for
# a real key, which is always an absolute path or "".
UNSTAMPED = "<unstamped>"


def load_state(path, key):
    """The snapshot at `path`, accepted only if account `key` wrote it.

    Returns (state, foreign). An accepted snapshot is (state, None). A rejected
    one is ({}, foreign), where `foreign` is the stamp found, or UNSTAMPED. The
    {} is load-bearing: another account's usage is not a lower bound on ours,
    so handing its buckets to decide(degraded=True) would brake "with full
    confidence" on numbers that say nothing about this account. With {} the
    age is None, so `run` refreshes, and a failed refresh leaves decide with no
    usable bucket -- blind, which is the honest answer.

    A missing or unparseable file, or one that parses to something other than
    a dict, is ({}, None): absent, not foreign. Otherwise every fresh account's
    first call would log a foreign snapshot, and `status` would blame another
    account for a file nobody has written yet.

    A snapshot with no `config_key` predates the stamp, and is trusted under
    the default key only. That keeps a single-account upgrade working, and
    costs a non-default account one refresh to replace it.

    Both arguments are explicit and no module global is read, so the hook and
    `status` each pass the paths their own tests pin. Writes no log: `run`
    does that, once per invocation, and `status` must never touch hook.log.
    """
    state = load_json(path, None)
    if not isinstance(state, dict):
        return {}, None
    if "config_key" not in state:
        return (state, None) if key == "" else ({}, UNSTAMPED)
    stamp = state["config_key"]
    if stamp == key:
        return state, None
    # A string compare and nothing else: no import, no syscall. A stamp that
    # is not a string came from no niceclaude, so it is shown, not trusted.
    return {}, stamp if isinstance(stamp, str) else repr(stamp)


def describe_key(key):
    """A stamp as a person reads it: the default account's key is "", which
    would print as an empty pair of parentheses."""
    return key if key else "<default>"


def log(msg):
    line = f"{time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime())} {msg}\n"
    try:
        try:
            with open(HOOK_LOG_PATH, "a", encoding="utf-8") as fh:
                fh.write(line)
        except FileNotFoundError:
            # A non-default account's directory is made only by install, watch,
            # sample, or a refresh that got as far as sampling (append_log runs
            # whether or not the sample parsed), so the lines from a refresh
            # that cannot start at all, or a brake or a fail-open before any
            # refresh has run, would otherwise vanish exactly then. Only on the miss, so the common
            # path pays no extra syscall.
            os.makedirs(os.path.dirname(HOOK_LOG_PATH), exist_ok=True)
            with open(HOOK_LOG_PATH, "a", encoding="utf-8") as fh:
                fh.write(line)
    except OSError:
        pass


def resolve(policy, cwd):
    """Longest matching path prefix wins, so subfolders inherit and a deeper
    rule can override a shallower one.

    Compares whole path components: a raw string prefix would let /foo/bar
    match /foo/barbaz and silently pace the wrong tree. A rule on the
    filesystem root is a legitimate catch-all and matches everything below it.
    """
    best = None
    for raw, entry in (policy.get("paths") or {}).items():
        try:
            key = norm_path(raw)
        except (OSError, ValueError):
            continue
        if path_within(cwd, key):
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


# --- per-call model detection (`--model detect`) ----------------------------
#
# No hook payload carries the model, but every caller has a transcript, and
# its last assistant record says which model wrote it. A folder that declares
# `detect` is paced on that, call by call, so an Opus organizer and its Fable
# subagents each answer to their own per-model bucket. A port of the bash
# proof of concept in open-questions.md section 9; model-detection-plan.md
# holds the decisions cited as D1-D9 below.

# The stored value of `--model` that asks for detection. Matched
# case-insensitively, like every other declared model.
DETECT = "detect"

# Read sizes for the backwards scan. Looked up at call time, so tests can
# shrink them. The cap bounds the cost of a transcript that has gone a long
# way without an assistant record: the longest such run measured was 2.5 MB,
# so 8 MiB is generous, and past it the answer is "unknown" rather than a
# hook that reads a whole transcript (D6).
DETECT_BLOCK = 64 * 1024
DETECT_CAP = 8 * 1024 * 1024

# `message.model` on records Claude Code writes itself rather than a model.
# It names no bucket, so accepting one would silently drop the model window;
# the scan steps past it and keeps going (D4).
SYNTHETIC = "<synthetic>"


def model_family(model_id):
    """The word `model_matches` compares, from a model id, or None.

    claude-fable-5-1 -> fable, claude-haiku-4-5-20251001 -> haiku,
    us.anthropic.claude-opus-5-5 -> opus. The first purely alphabetic token
    after `claude`, because the version and date tokens around it vary and
    the bucket's display name only ever carries the family.
    """
    if not isinstance(model_id, str):
        return None
    tokens = model_id.lower().replace(".", "-").replace("_", "-").split("-")
    if "claude" not in tokens:
        return None
    for tok in tokens[tokens.index("claude") + 1:]:
        if tok.isascii() and tok.isalpha():
            return tok
    return None


def _record_model(line):
    """`message.model` of one transcript line if it is a usable assistant
    record, else None. Torn, foreign or garbled lines are simply not one."""
    # Cheap prefilter: most lines are tool results and user turns, and
    # parsing them only to throw them away is most of the cost of a scan.
    if b'"assistant"' not in line:
        return None
    try:
        # Decoded leniently, so one stray non-UTF-8 byte inside a message
        # does not cost the record its model. A trailing \r (CRLF) is just
        # whitespace to json.
        rec = json.loads(line.decode("utf-8", "replace"))
    except (ValueError, RecursionError):
        return None
    if not isinstance(rec, dict) or rec.get("type") != "assistant":
        return None
    msg = rec.get("message")
    model = msg.get("model") if isinstance(msg, dict) else None
    if not isinstance(model, str) or not model or model == SYNTHETIC:
        return None
    return model


def _last_model(path):
    """Scan `path` backwards for the newest usable assistant record.

    The replacement for the PoC's `tac | jq | head -1`. Blocks are read from
    the end; each block's first line may be the tail of a longer one, so its
    bytes are kept pending rather than parsed. Pending blocks are only joined
    when a newline arrives to end the line, so a long line costs one join,
    not one per block: re-joining a growing carry on every block made a
    multi-megabyte line quadratic. The file's first line is parsed on its own
    only once the scan reaches the start, where it is whole.
    """
    block = DETECT_BLOCK
    cap = DETECT_CAP
    with open(path, "rb") as fh:
        fh.seek(0, os.SEEK_END)
        pos = fh.tell()
        pending = []     # blocks of the current partial line, newest first
        spent = 0
        while pos > 0 and spent < cap:
            n = min(block, pos, cap - spent)
            pos -= n
            fh.seek(pos)
            buf = fh.read(n)
            spent += n
            pending.append(buf)
            if b"\n" not in buf:
                continue
            lines = b"".join(reversed(pending)).split(b"\n")
            pending = [lines[0]]
            # Newest first. The very last line of the file may be torn by a
            # write in progress; it fails to parse and is skipped like any
            # other non-record.
            for line in reversed(lines[1:]):
                model = _record_model(line)
                if model:
                    return model
        if pos == 0:
            return _record_model(b"".join(reversed(pending)))
    # Out of budget with the file not exhausted: unknown, not a guess.
    return None


def _transcript_of(payload):
    """The caller's own transcript, or None if it cannot be found.

    The main agent's is `transcript_path`. A subagent's lives under the
    session's directory beside it; when the derived path is missing, the
    directory is searched, as the PoC's `find` does. A subagent is never
    given its parent's transcript (D3): the parent's model is exactly the
    wrong answer in the case this exists for.
    """
    main = payload.get("transcript_path")
    if not isinstance(main, str) or not main:
        return None
    agent = payload.get("agent_id")
    if not agent:
        return main
    if not isinstance(agent, str) or "/" in agent or os.sep in agent:
        return None
    base = os.path.dirname(main)
    name = f"agent-{agent}.jsonl"
    sid = payload.get("session_id")
    if isinstance(sid, str) and sid and "/" not in sid and os.sep not in sid:
        primary = os.path.join(base, sid, "subagents", name)
        if os.path.isfile(primary):
            return primary
    if not base:
        return None
    for root, _dirs, files in os.walk(base):
        if name in files:
            return os.path.join(root, name)
    return None


def detect_model(payload):
    """The model id of whoever made this hook call, or None.

    Never raises. A detector that raised would fail the hook open -- `main`
    lets every call through on an exception -- so anything at all that goes
    wrong here means "unknown", which costs only the per-model bucket for
    this one call (D1, D6).
    """
    try:
        if not isinstance(payload, dict):
            return None
        # A subagent's transcript does not exist yet at SubagentStart (Phase
        # 0, every run). Looking would always fail, and the fallback search
        # would walk the whole project directory on every fan-out, so do not
        # look at all (D2). Its first tool call is detected instead.
        if payload.get("hook_event_name") == "SubagentStart":
            return None
        path = _transcript_of(payload)
        return _last_model(path) if path else None
    except Exception:
        return None


def wants_detection(policy, entry):
    """Does this rule pace on the caller's model -- `detect`, with the model
    window enforced? Only then is a transcript worth opening (D5)."""
    model = entry.get("model")
    if not isinstance(model, str) or model.lower() != DETECT:
        return False
    defaults = policy.get("defaults") or {}
    return "model" in normalize_enforce(entry.get("enforce",
                                                  defaults.get("enforce")))


def model_tag(policy, entry, caller_model):
    """`model=<family>(detected|declared)` or `model=none`, for hook.log (D7).

    Says which per-model bucket a hold could have answered to, so `none`
    also covers a rule that does not enforce the model window at all."""
    defaults = policy.get("defaults") or {}
    if "model" not in normalize_enforce(entry.get("enforce",
                                                  defaults.get("enforce"))):
        return "model=none"
    model = entry.get("model")
    model = model.lower() if isinstance(model, str) else ""
    if model == DETECT:
        return (f"model={caller_model}(detected)" if caller_model
                else "model=none")
    return f"model={model}(declared)" if model else "model=none"


def decide(policy, state, cwd, now, degraded=False, event=None, hard=False,
           caller_model=None):
    """Return {'paced':..,'braked':..,'wake_at':..,'reason':..,'blind':..,
    'chunk':..,'max_delay':..,'band_delay':..,'blind_delay':..,'region':..,
    'hold':..,'expired':..}.

    'expired' says at least one ENFORCED bucket's window has rolled since the
    snapshot was taken, so its reading is void. `run` reads it to force one
    refresh: an expired reading voids CONTENT without aging the FILE, so the
    freshness gate there cannot otherwise see it. In practice only the hot
    branch can report it True, because an expired bucket is always hot
    (bucket_pace returns it as `over` with no wake, and the loop below files
    it accordingly).

    It is nonetheless present on EVERY paced verdict, False included, and the
    distinction from 'wake_at' and 'reason' -- which are legitimately absent
    when nothing brakes -- is worth keeping rather than tidying away. Those
    two are facts about a hold, and with no hold there is no fact. 'expired'
    is a property of the SNAPSHOT, and a snapshot always has an answer to it.
    The same argument bucket_pace's own return shape is built on applies here:
    hook.main catches every exception and fails open, so one consumer reading
    d["expired"] on a branch that omitted it would not misreport a verdict, it
    would stop pacing the folder. The unpaced early-out above is the one
    exception, and it is not a verdict at all -- it carries no region, chunk
    or blind_delay either, and every caller tests `paced` first.

    'region' is geometric -- 'free', 'band' or 'over' -- and is reported even
    when nothing brakes, because `run` uses it to decide how fresh the snapshot
    has to be before it dares let a step through.

    'hold' is what to do about it: None, 'soft' (cappable at band_delay) or
    'hard' (only max_delay may cut it short -- and blind_delay, if 'blind').

    'blind' marks a hard hold that THE BLIND RULE (in the body, above the
    `if hot:` branch) finds unjustified: no governing bucket -- the hot
    entries, plus the banded ones when `hard` is latched -- has a wake
    strictly after `now`. It is what `run` lets blind_delay release.

    `hard` is the CALLER'S MEMORY, and it is what makes the band hysteresis
    real. This function is stateless: it reclassifies from scratch every pass,
    so a bucket held above the brake line stops being 'over' the instant the
    line rises past `pess` -- which is the old release point, the one with no
    headroom behind it. Left to itself it would then release, and the whole
    band would be decoration. Passing `hard=True` says "this hold began above
    the brake line", and a hold that began there runs all the way down to the
    throttle line rather than stopping at the brake line on the way past.
    `run` latches it only on a hard hold that is not blind: a blind hold has
    seen no line to begin above.

    `degraded` means the snapshot is older than MAX_STALE and could not be
    refreshed -- we are flying blind. Stale data is not useless, though: usage
    only ever rises within a window, so an old reading is a LOWER bound on
    current consumption. That asymmetry is the whole policy here --

        stale data can justify BRAKING, but never ALLOWING.

    So a degraded snapshot that already says "over the line" is acted on with
    full confidence while its wake is still ahead (it can only have got worse)
    -- unless its window has reset since the reading, which voids the lower
    bound and makes the hold blind (see THE BLIND RULE). A degraded snapshot
    that says "under the line" is treated as unknown and brakes anyway.

    Note there is no truthiness trap here: the jq version's `//` operator falls
    back on false as well as null, which silently disabled the kill switch and
    would have turned a configured m0 of 0 into 5. dict.get with a default only
    fires on a missing key.

    `caller_model` is the caller's model family, as `run` detected it, or
    None. It is read only under a rule that declares `detect`.
    """
    entry = paced_entry(policy, cwd)
    if entry is None:
        return {"paced": False}

    # Every one of these is coerced, because every one of them comes out of a
    # file a human edits. See coerce_num: the failure it prevents is not a
    # crash but a folder that fails open on every tool call while still
    # reporting itself paced.
    defaults = policy.get("defaults") or {}
    m0 = coerce_num(entry.get("m0", defaults.get("m0", DEFAULT_M0)), DEFAULT_M0)
    m1 = coerce_num(entry.get("m1", defaults.get("m1", DEFAULT_M1)), DEFAULT_M1)
    # A fan-out is a much larger commitment than one more step of work already
    # in flight, so SubagentStart may be held to a stricter line than
    # PreToolUse. Raising the reserve raises the bar without touching the
    # control law itself.
    if event == "SubagentStart":
        m1 += coerce_num(entry.get("fanout_reserve",
                                   defaults.get("fanout_reserve",
                                                DEFAULT_FANOUT_RESERVE)),
                         DEFAULT_FANOUT_RESERVE)
    # Floored at 1s: a chunk of 0 would make the nap zero whenever no cap is in
    # force, and the loop would spin at full tilt instead of sleeping.
    chunk = max(1.0, coerce_num(entry.get("chunk",
                                          defaults.get("chunk", DEFAULT_CHUNK)),
                                DEFAULT_CHUNK))
    # Resolved here rather than once at the start of a brake, so that -- like
    # every other knob -- a policy edit reaches an already-frozen agent within
    # one chunk instead of only on its next tool call.
    max_delay = off_or_num(entry, defaults, "max_delay", DEFAULT_MAX_DELAY)
    if max_delay is not None and max_delay < 0:
        max_delay = 0
    # The throttle line sits `band` points under the brake line. `band_delay`
    # is what one hold costs while between them; written null it is pure
    # release hysteresis and is run through at full speed. Both ship set, so
    # both are read with off_or_num: a null in the rule means off, and has to
    # survive a default that is no longer off itself. bucket_pace reads a null
    # band as zero width.
    band = off_or_num(entry, defaults, "band", DEFAULT_BAND)
    band_delay = off_or_num(entry, defaults, "band_delay", DEFAULT_BAND_DELAY)
    if band_delay is not None and band_delay <= 0:
        # No throttle is no throttle. Read as "unset" rather than as an instant
        # release, which would write a throttle/release pair to hook.log on
        # every tool call for a hold that never happened.
        band_delay = None
    # The ceiling on a hold that cannot see. Surfaced on every verdict, blind
    # or not, because `run` re-reads it each pass and a hold can turn blind
    # mid-way; resolved in _shared so `status` reads the same number.
    blind_delay = blind_delay_of(entry, defaults)
    model = declared = (entry.get("model") or "").lower()
    if declared == DETECT:
        # `caller_model` is the family `run` read from the caller's own
        # transcript. Passed in rather than read here, so this stays pure.
        # Unknown means no per-model bucket for this call; session and week
        # still apply (D1). Under any declared model it is ignored.
        model = (caller_model or "").lower()

    # Which windows this folder answers to. A project you are actively tending
    # may want the 5-hour line to smooth it out while ignoring the weekly line,
    # which exists to protect budget for days you are not here.
    enforce = normalize_enforce(entry.get("enforce", defaults.get("enforce")))
    buckets = state.get("buckets") or {}
    enforced = [
        (k, b) for k, b in buckets.items()
        if (k == "session" and "session" in enforce)
        or (k == "week:all models" and "week" in enforce)
        or ("model" in enforce and model_matches(k, model))
    ]
    if not enforced and buckets and enforce == {"model"}:
        # With `model` the only window enforced, a call that matches no
        # per-model row is bound by nothing: "nothing applies", not "cannot
        # see", since the snapshot is there and was read. That covers a model
        # with no bucket at all (declared `opus`; no week:Opus exists), an
        # undeclared model, and under `detect` an Opus call, an unknown model
        # or a SubagentStart. Falling through would brake it blind on every
        # call -- once until the harness timeout (open-questions 12), now for
        # a blind_delay apiece, which is still a hold for nothing.
        #
        # A missing per-model row is ignored even for a model that has one.
        # The renderer drops a row whose utilization is null, so a Fable call
        # with no week:Fable row may have one that is merely unrendered; it
        # runs free rather than freezing. The user chose that over a list of
        # which families have buckets (model-detection-plan.md, D9).
        #
        # An empty snapshot still goes blind below, and so does one missing
        # an enforced session or week bucket (the cp1252 misparse in
        # sample_once leaves only week:Fable): that is "cannot see".
        return {"paced": True, "braked": False, "hold": None, "chunk": chunk,
                "max_delay": max_delay, "band_delay": band_delay,
                "blind_delay": blind_delay,
                "region": "free", "blind": False, "expired": False}
    if not enforced:
        return {"paced": True, "braked": True, "hold": "hard",
                "wake_at": now + chunk,
                "reason": "no usable buckets in snapshot", "chunk": chunk,
                "max_delay": max_delay, "band_delay": band_delay,
                "blind_delay": blind_delay,
                "region": "over", "blind": True, "expired": False}

    # Kept apart, because the two demand different things. An `over` bucket
    # demands a hold until ITS throttle line catches up, which can be hours. A
    # `band` bucket only ever demands one band_delay. Pooling them would let a
    # bucket that is merely in the band -- and with band_delay unset would not
    # stop anything at all -- drag someone else's hold out to its own throttle
    # crossing. On the weekly line that is ~1.94h per band point, which is a
    # plausible way to push a hold past the hook's registered timeout, at
    # which point the harness kills it and the agent proceeds UNPACED.
    hot = []
    banded = []
    # Does any enforced bucket describe a window that has already rolled? Only
    # `run` reads this, and only to force a refresh (E6, below).
    expired = False
    for key, b in enforced:
        # The pace-line arithmetic lives in _shared so `status` can report the
        # same numbers this brakes on, rather than a second implementation of
        # them that drifts.
        p = bucket_pace(b, now, m0, m1, band)
        if p is None:
            # Unusable reads as the most restrictive region, not the most
            # convenient one: we cannot see where this bucket stands.
            # The fourth field says whether this entry's wake is a real one:
            # False here and on the floor below, where it is only the next
            # look, and on a wake at or before `now` (see the over-line case).
            # A hot hold with no real wake anywhere is blind.
            hot.append((key, f"{key}: unusable", now + chunk, False))
            continue
        if p["expired"]:
            # The window rolled since this reading was taken, so the reading
            # bounds nothing in the window now running: not a lower bound on
            # usage (the window was zeroed) and not evidence of headroom (the
            # new window is unobserved). Re-checked on the chunk, and marked
            # unsupported in the fourth field, so THE BLIND RULE below calls
            # the resulting hold what it is -- blind, which is already this
            # codebase's answer to "cannot see".
            #
            # Handled here, FIRST and explicitly, rather than left to the
            # wake-less arm below that bucket_pace's forced `over` region
            # would otherwise route it into. That arm would log "session 95%
            # over floor 5%", which is simply false -- the bucket is not over
            # any floor, its window is gone -- and hook.log is read by eye.
            # Being first also means this does not depend on that forced
            # region staying forced.
            expired = True
            hot.append((key, f"{key}: window reset since this reading",
                        now + chunk, False))
            continue
        if p["region"] == "free":
            continue
        if p["wake"] is None:
            # No reset clause, so the line is unsolvable and we judged against
            # the m0 floor. Re-check on the chunk until the clause reappears.
            # :g so a coerced m0 reads "5%" rather than "5.0%" -- these strings
            # go straight into hook.log, which is read by eye.
            hot.append((key, f"{key} {p['pct']}% over floor {m0:g}%",
                        now + chunk, False))
        elif p["region"] == "over":
            # Real only while it is still ahead of us. bucket_pace clamps the
            # wake to the window's reset, so a wake at or before `now` means
            # the window has ALREADY reset since this reading: the snapshot
            # predates the zeroing, is no longer a lower bound on anything,
            # and a stale one with the refresh failing would otherwise brake
            # "with confidence" for as long as the outage lasts. Nor is a past
            # wake one to aim at: as wake_at it would make `run` nap the 1s
            # floor for the whole hold, so re-check on the chunk instead, as
            # every other wake-less entry does.
            #
            # Kept, and deliberately, now that the expired arm above catches
            # the gross case. What still reaches here is the jitter zone: a
            # reading inside RESET_JITTER of its own reset is NOT voided (the
            # server rounds the times it prints, so that window may well still
            # be running), its elapsed is clamped to 1.0, and its wake then
            # solves at or before the reset it is clamped to. This is also the
            # guard that would catch a future regression in that boundary, and
            # it costs one comparison.
            real = p["wake"] > now
            hot.append((key, f"{key} {p['pct']}% over line {p['allowed']:.1f}%",
                        p["wake"] if real else now + chunk, real))
        else:
            banded.append((key,
                           f"{key} {p['pct']}% in band (line {p['allowed']:.1f}%, "
                           f"throttle {p['low']:.1f}%)", p["wake"]))

    # Most restrictive bucket wins. This is the GEOMETRIC region -- where we
    # stand, not what we do about it. It is reported even when we do not brake,
    # because `run` uses it to decide how fresh the snapshot has to be.
    region = "over" if hot else "band" if banded else "free"

    # THE BLIND RULE. Every hard hold below is classified by this, and `run`
    # cites it rather than restating it:
    #
    #   A hard hold is blind if and only if no bucket that governs it has a
    #   wake strictly in the future that its reading supports.
    #
    # The governing buckets are the hot entries, plus the banded entries once
    # `hard` is latched (a descent from over the line is still owed). A
    # wake-less entry supports nothing, and there are now three kinds:
    # unusable, over the m0 floor with no reset clause, and EXPIRED -- the
    # window rolled under the reading, so it bounds nothing in the window now
    # running. The expired arm above files that one.
    #
    # A wake at or before `now` is not supported either, and note what it now
    # means. It used to mean "the window has reset since the reading", but a
    # window that has reset is voided to expired above and publishes no wake
    # at all. What is left is the JITTER ZONE: a reading within RESET_JITTER
    # of its own printed reset, kept on purpose because the server rounds
    # those times and the window may well still be running. Its elapsed is
    # clamped to 1.0, so its wake solves to the reset just behind us. We
    # cannot see past that roll either, so it supports nothing -- but it is a
    # different fact about a different reading, and a future change to the
    # boundary would change which readings land here.
    #
    # Staleness does NOT enter into it. A stale pct is a lower bound (usage
    # only rises within a window), so a stale reading above a line proves the
    # true one is above it too, and any wake it gives is finite. That is
    # reason enough to HOLD. It is never reason to PROCEED, which is why the
    # soft-band branch, and only it, refuses stale data.
    supported = any(h[2] > now for h in banded) if hard else False

    if hot:
        # Confident even when degraded: consumption only rises, so a stale
        # reading that is already over the line is a floor, not a guess. The
        # wake comes from the over buckets alone; a band bucket is named in the
        # reason so the log tells the whole story, but does not lengthen this.
        #
        # Blindness by THE BLIND RULE above: a hot entry's fourth field says
        # whether its own wake is supported, and a latched descent's band
        # wakes count as well -- so an unusable sibling appearing mid-descent
        # does not turn a justified hold into a blind one. One supported wake
        # anywhere and it is not blind, however wake-less the rest.
        seen = any(h[3] for h in hot) or supported
        return {"paced": True, "braked": True, "hold": "hard",
                "wake_at": max(h[2] for h in hot),
                "reason": "; ".join(h[1] for h in hot + banded),
                "chunk": chunk, "max_delay": max_delay,
                "band_delay": band_delay, "blind_delay": blind_delay,
                "region": region, "blind": not seen, "expired": expired}

    # A hold that began above the brake line does not end at it.
    #
    # This is the branch the band exists for, and it is easy to lose: by the
    # time the brake line has risen past `pess` the bucket classifies as
    # `banded`, and every other rule here would let it go -- at exactly the
    # zero-headroom release point the band was added to avoid. `hard` is the
    # only thing that distinguishes "we are passing through the band on our way
    # down from a real brake" from "we wandered into the band from below", and
    # those two deserve opposite answers.
    #
    # Note the cap: this is a `hard` hold, so band_delay has no say in it. The
    # agent is coming down from over the line and the only knob that may cut
    # that short is max_delay, exactly as before the band existed.
    #
    # Taken whenever THE BLIND RULE above finds a supported band wake, stale
    # or not: a stale reading still above the throttle line proves the true
    # one is too, so the descent is still owed and is not blind. A latched
    # hold whose band wakes have all passed (the window reset under a stale
    # reading) supports nothing, and falls through to the branches below.
    if banded and supported:
        return {"paced": True, "braked": True, "hold": "hard",
                "wake_at": max(h[2] for h in banded),
                "reason": "; ".join(h[1] for h in banded) + " [running down]",
                "chunk": chunk, "max_delay": max_delay,
                "band_delay": band_delay, "blind_delay": blind_delay,
                "region": region, "blind": False, "expired": expired}

    # Two ways a band stops being a reason to hold.
    #
    # With no band_delay it is pure hysteresis: it deepens the hold the brake
    # line triggers, but is not itself a reason to stop. Run through it at full
    # speed and let the brake line do the stopping.
    #
    # And a band read off a snapshot we know to be stale is not a band at all.
    # "Proceed, but slowly" is still proceeding, and proceeding is the one
    # thing stale data can never justify -- the whole asymmetry this function
    # is built on. Falling through hands it to the degraded branch below, which
    # holds blind and lets out only max_delay and blind_delay: the same
    # treatment a stale "under the line" gets. Without this, band_delay would
    # quietly become a third knob that proceeds while blind, a property that
    # belongs to max_delay (opt-in) and blind_delay (bounded, and logged as a
    # blind-release) alone.
    if banded and band_delay is not None and not degraded:
        # A band wake can be in the PAST, and this is the only arm with no
        # guard against publishing one. Inside the jitter zone a reading is
        # kept rather than voided, takes the elapsed clamp, and then solves
        # its release to the reset just behind us -- `band` implies pess >
        # low, which past the clamp means the solve runs beyond the window and
        # is clipped back to `resets`. Unguarded, `run` computed
        # `nap = min(chunk, max(1.0, wake_at - now))`, took the 1s floor, and
        # spun ~180 passes at a second apiece -- re-reading policy.json and
        # state.json on every one -- for a hold whose length band_delay had
        # already fixed. Churn rather than a wrong hold, but it is what plan
        # section 1.3 was written to remove, and it is reachable for 120s
        # after EVERY roll.
        #
        # CONDITIONAL, mirroring the hot arm's `real`, and that matters: a
        # band wake only a few seconds out is ordinary, not pathological. The
        # session line rises about a point every 207s, so a crossing inside
        # the default 15s chunk is common, and flooring it unconditionally
        # would round every one of those up to a full chunk -- releasing late
        # by up to chunk, where the unguarded code at least timed them
        # exactly.
        #
        # Note what the substitute IS, because `status` is handed it too: a
        # re-check time, not a crossing. Its band branch would otherwise
        # print it as "full speed resumes in 15s, when the throttle line
        # catches up", which in the jitter zone is a confident falsehood --
        # that line cannot catch up, band_delay is what releases the hold.
        # cmd_status has its own guard for exactly this case; keep the two
        # together if either moves.
        #
        # Replaced HERE and not in the `banded` tuples, deliberately:
        # `supported` above reads that same field, and rewriting it to a
        # synthetic future wake would un-blind a latched descent and route it
        # into the uncapped running-down branch. If those entries ever do need
        # a different wake, they need a fourth `real` field as `hot` has.
        band_wake = max(h[2] for h in banded)
        return {"paced": True, "braked": True, "hold": "soft",
                "wake_at": band_wake if band_wake > now else now + chunk,
                "reason": "; ".join(h[1] for h in banded),
                "chunk": chunk, "max_delay": max_delay,
                "band_delay": band_delay, "blind_delay": blind_delay,
                "region": region, "blind": False, "expired": expired}

    if degraded:
        # Under the line according to data we know to be out of date. That is
        # not evidence of headroom, so hold rather than guess. Logged
        # distinctly: "braked because blind" and "braked because hot" are
        # completely different problems at 3am.
        # Reported as "over", and deliberately: "I cannot see" must not resolve
        # to "I am comfortably below the brake line", which is exactly what
        # letting band_delay release this would assert. What frees a blind
        # agent is max_delay, the knob documented as proceeding while over the
        # line, or blind_delay, the one that exists for exactly this case.
        age = int(now - (state.get("ts_epoch") or now))
        return {"paced": True, "braked": True, "hold": "hard",
                "wake_at": now + chunk,
                "reason": f"BLIND: snapshot {age}s old and refresh failing",
                "chunk": chunk, "max_delay": max_delay,
                "band_delay": band_delay, "blind_delay": blind_delay,
                "region": "over", "blind": True, "expired": expired}

    return {"paced": True, "braked": False, "hold": None, "chunk": chunk,
            "max_delay": max_delay, "band_delay": band_delay,
            "blind_delay": blind_delay, "region": region, "blind": False,
            "expired": expired}


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


def run(cwd, event=None, payload=None):
    # `payload` is the hook's stdin, read only to detect the caller's model.
    brake_start = None
    started = time.time()  # the invocation's own start, for blind_start
    blind_start = None   # when this run of continuous blindness began; None
                         # while the hold (if any) is one data justifies
    caller = None        # the caller's model family, under `detect` only
    detected = False     # detection is tried at most once per invocation: a
                         # frozen agent cannot change model while frozen (D5)
    fails = 0            # consecutive failed refreshes, indexes REFRESH_BACKOFF
    next_try = 0.0       # earliest time we may attempt another refresh
    was_blind = False
    first = True         # only the first pass demands a near-line-fresh read
    hard = False         # this hold began above the brake line, so it runs all
                         # the way down to the throttle line rather than
                         # stopping at the brake line on its way past. `decide`
                         # is stateless, so this latch is the hysteresis.
    was_soft = False
    told_foreign = False  # `foreign snapshot` is logged once per invocation,
                          # not once per chunk of a hold that cannot refresh

    def load():
        # STATE_PATH and ACCOUNT_KEY are looked up here, at call time, so the
        # tests' monkeypatches of this module reach both load sites.
        nonlocal told_foreign
        state, foreign = load_state(STATE_PATH, ACCOUNT_KEY)
        if foreign is not None and not told_foreign:
            told_foreign = True
            log(f"foreign snapshot ({describe_key(foreign)}) cwd={cwd} "
                f"-- ignored")
        return state

    while True:
        now = time.time()
        pass_start = now     # before any refresh this pass makes
        policy = load_json(POLICY_PATH, {})

        # Cheapest possible gate, and it must come first: an unpaced folder
        # needs no usage data, so it must never trigger a refresh. This is the
        # common case -- every foreground session, on every tool call.
        entry = paced_entry(policy, cwd)
        if entry is None:
            return brake_start, "unpaced"

        # After the gate, so an unpaced folder never opens a transcript, and
        # only for a rule that asks for it. Re-checked each pass so a policy
        # switched to `detect` mid-hold is honoured, but tried only once.
        if not detected and wants_detection(policy, entry):
            detected = True
            caller = model_family(detect_model(payload))

        # Another account's snapshot loads as {}, so its age is None and the
        # refresh below runs: one refresh replaces it with our own.
        state = load()
        age = snapshot_age(state, now)
        degraded = age is None or age > MAX_STALE
        d = decide(policy, state, cwd, now, degraded=degraded, event=event,
                   hard=hard, caller_model=caller)
        # Whether this pass was blind BEFORE any refresh. A hold that turns
        # blind mid-way turns at this decide, not the one after the refresh,
        # so the refresh below is blind time and must be on the blind clock.
        pre_blind = bool(d.get("blind"))

        # How fresh the snapshot must be depends on where the last one said we
        # stand. Well under the throttle line, MAX_STALE is plenty. At or above
        # it we are about to let a step through -- either straight away or
        # after one band_delay -- and a single step can climb a long way, so
        # look again first. Only on the first pass: the hold that follows is
        # time this agent spends frozen, and re-reading through it would buy
        # nothing but process churn. One refresh per invocation is exactly the
        # guarantee wanted, because every completed step is followed by a fresh
        # invocation.
        limit = (NEAR_STALE if first and d.get("region") in ("band", "over")
                 else MAX_STALE)
        # No refresh once the blind allowance is spent. The last nap lands
        # exactly on the blind deadline, and a refresh started now is
        # synchronous and can take up to its own 180s timeout before the
        # release check below ever runs -- so the call would cost blind_delay
        # plus a refresh, the overrun this knob exists to bound. The next
        # tool call refreshes anyway.
        #
        # Judged from the pre-refresh decide, on the clock as it will stand
        # once this pass is counted: a hold turning blind here starts it at
        # pass_start. Not on the first pass, whose refresh is what may cure a
        # merely stale snapshot -- skipping it there (blind_delay 0 is the case
        # that could) would leave every call blind with nothing ever looking.
        clock = blind_start
        if clock is None and pre_blind and brake_start is not None:
            clock = pass_start
        spent = (clock is not None and pre_blind
                 and d.get("blind_delay") is not None
                 and now >= clock + d["blind_delay"])
        # An expired reading forces a refresh whatever the file's age says.
        # It voids CONTENT without aging the FILE, so the freshness gate
        # cannot see it: `limit` only tightens to NEAR_STALE once the region
        # is already band or over, and a snapshot younger than that is never
        # re-read at all. So a snapshot under NEAR_STALE old that straddles
        # the roll stalls at the exact moment a fresh window became
        # available, for a full blind_delay, where a ~2s refresh would have
        # cured it -- what the refresh reaches is the post-roll sample's
        # MISSING reset clause, which bucket_pace already judges against the
        # m0 floor.
        #
        # One attempt, and only one, whether it succeeds or fails: `first` is
        # cleared immediately after this block, so a forced refresh that fails
        # gets no retry from the force. Later passes fall back to the ordinary
        # `age > limit` gate -- `limit` is MAX_STALE once `first` is False --
        # and to the next_try backoff. That is the intended division of
        # labour: the force exists to look ONCE at the moment the content went
        # void, and chasing an unreachable `/usage` every chunk is exactly
        # what the backoff exists to prevent.
        #
        # Gated on `first`, and that gate is load-bearing: a successful
        # refresh sets next_try back to 0, so an ungated force would re-run a
        # ~2s subprocess on every chunk for as long as the window stayed
        # expired. One refresh per invocation is the guarantee the rest of
        # this block already makes, and it remains the right one -- every
        # completed step is followed by a fresh invocation.
        if ((age is None or age > limit or (first and d.get("expired")))
                and now >= next_try and not spent):
            if refresh_snapshot():
                fails = 0
                next_try = 0.0
            else:
                fails = min(fails + 1, len(REFRESH_BACKOFF) - 1)
                next_try = now + REFRESH_BACKOFF[fails]
            now = time.time()
            state = load()
            age = snapshot_age(state, now)
            degraded = age is None or age > MAX_STALE
            d = decide(policy, state, cwd, now, degraded=degraded, event=event,
                       hard=hard, caller_model=caller)
        first = False

        if not d.get("paced"):
            # Kill switch or policy change, possibly mid-brake.
            return brake_start, "unpaced"
        if not d.get("braked"):
            return brake_start, "line-caught-up"

        # Latch. Once a hold is hard it stays hard for the rest of this
        # invocation, which is what carries it down through the band.
        #
        # Only a hold that data justifies sets it -- one `decide` does not mark
        # blind, under THE BLIND RULE stated there. A blind hold is hard too,
        # but it never saw the brake line, so it has nothing to run down
        # from: latched, the next pass would let any band bucket with a
        # future wake "justify" it as a descent, un-blind it, reset the blind
        # clock, and hold until that band wake -- hours, on the weekly line.
        # A hold that DID begin over the line keeps its latch through a later
        # blind spell, which is intended: the descent is still owed.
        soft = d.get("hold") == "soft"
        if not soft and not d.get("blind"):
            hard = True

        # The blind clock, which runs while `decide` says blind (THE BLIND
        # RULE, stated there). What it bounds is continuous blindness within
        # this one tool call, so that no call ever costs more than blind_delay
        # of it. Blind from the first pass, it starts at the start of `run`:
        # the refresh that just failed was time spent not seeing, and leaving
        # it out would let a slow failing refresh stretch every blind hold.
        # Turned blind mid-hold, it starts at the start of the pass that
        # first saw it, for the same reason; data that justifies the hold
        # again resets it, since a real brake is not blindness and must not
        # use up the allowance of the next stretch that is. A refresh in
        # flight is synchronous and can still overrun it by up to its own
        # timeout.
        if d.get("blind"):
            if blind_start is None:
                # Mid-hold, from the start of the pass that first saw it, so
                # a failed refresh in that pass is counted; if only the
                # post-refresh decide is blind, from now.
                blind_start = (started if brake_start is None
                               else pass_start if pre_blind else now)
        else:
            blind_start = None

        if brake_start is None:
            brake_start = now
            was_blind = d.get("blind", False)
            was_soft = soft
            # Band holds get their own verb. They are one per tool call, so
            # they would otherwise swamp hook.log and drown the invariant that
            # an unmatched `brake` means the harness killed the hook at its
            # registered timeout -- which is the only way that failure is ever
            # visible. `grep ' brake '` still counts real brakes.
            verb = "throttle" if soft else "brake  "
            # The model tag goes last, after the reason, so every reader that
            # matches on the verb or the cwd= field sees the line unchanged.
            log(f"{verb} cwd={cwd} [{event or 'PreToolUse'}] "
                f"{d.get('reason', '')} {model_tag(policy, entry, caller)}")
        elif was_soft and not soft:
            # A throttle that escalated: a sibling or the foreground session
            # pushed us over the brake line mid-hold, and this is now an
            # uncapped hold that can run to the harness timeout. It MUST be
            # logged under the brake verb -- the reader is told to treat an
            # unmatched `throttle` as noise, so without this the one hold that
            # can silently end in a kill is the one that looks least important.
            was_soft = False
            was_blind = d.get("blind", False)
            log(f"brake  cwd={cwd} [escalated from throttle] "
                f"{d.get('reason', '')} {model_tag(policy, entry, caller)}")
        elif d.get("blind", False) != was_blind:
            # Crossing between "over the line" and "cannot see" mid-brake is a
            # material change in why we are stopped; record it.
            was_blind = d.get("blind", False)
            log(f"brake* cwd={cwd} {d.get('reason', '')}")

        # Both caps cut a hold short, and which one applies is the whole point
        # of the second line.
        #
        # Either way the reason is the prompt cache: a hold long enough to
        # outlive the cache TTL means the next turn re-reads the whole context
        # from cold, so a wait taken to save budget can cost more than it
        # saved. What differs is where the money comes from.
        #
        # `band_delay` applies between the lines. It is a lower gear -- one
        # hold, then one step, then another hold -- taken while still safely
        # UNDER the brake line, so capping it gives up no restraint at all. It
        # buys cache warmth out of headroom not yet spent.
        #
        # `max_delay` applies above the brake line, and is unchanged. It
        # releases while STILL OVER, the agent takes one more step, and the
        # next PreToolUse brakes again -- the restraint survives as many short
        # holds rather than one long one, but the ceiling is what pays. It
        # remains the only place this tool deliberately proceeds while KNOWN to
        # be over the line, which is why it is opt-in and off by default.
        #
        # So the efficiency knob and the guarantee no longer fight: you can be
        # cache-warm in the band without spending the ceiling to get there.
        #
        # `blind_delay` is the third, and it is not about cost at all: it
        # bounds a hold that cannot see. With no reading that could justify
        # the hold, holding until one appears is a stall that could run to the
        # harness timeout -- and the rule is to fail free rather than stall
        # forever. It runs on its own clock (blind_start, above) rather than
        # brake_start's, because what it caps is the blind part of a hold, not
        # the whole; and every cap in force is a ceiling, so the tightest one
        # ends the hold. A confident brake -- over the line with a real wake,
        # however stale -- never sees it.
        #
        # With no cap in force the hold ends only when the throttle line
        # catches up, the folder is unpaced, or the harness kills the hook at
        # its registered timeout -- and that last one is silent, which is why
        # an unmatched brake in hook.log means "timed out", not "hung".
        max_delay = d.get("max_delay")
        band_delay = d.get("band_delay")
        blind_delay = d.get("blind_delay")
        cap = max_delay
        if soft and band_delay is not None:
            cap = band_delay if max_delay is None else min(band_delay, max_delay)
        deadline = None if cap is None else brake_start + cap
        blind_deadline = (None if blind_start is None or blind_delay is None
                          else blind_start + blind_delay)

        # Checked first, so a tie goes to the blind label: when both ceilings
        # fall on the same instant, "we could not see" is the truer account
        # of why the call went through, and the one a reader of hook.log has
        # to act on. When max_delay falls strictly first, its own
        # WHILE-BLIND label says the same with the knob that fired.
        if blind_deadline is not None and now >= blind_deadline and (
                deadline is None or blind_deadline <= deadline):
            return brake_start, "blind-release"
        if deadline is not None and now >= deadline:
            if soft:
                return brake_start, "band-release"
            return brake_start, ("max_delay-release-WHILE-BLIND"
                                 if d.get("blind") else "max_delay-release")

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
        if blind_deadline is not None:
            deadline = (blind_deadline if deadline is None
                        else min(deadline, blind_deadline))
        if deadline is not None:
            # Without this a cap shorter than the chunk would still sleep a
            # whole chunk, so `--max-delay 5` under the default 15s chunk would
            # hold for 15. Never below 1s: the release check above uses >=, so a
            # zero nap would spin.
            nap = max(1.0, min(nap, deadline - now))
        time.sleep(nap)


def main():
    try:
        raw = sys.stdin.read()
    except Exception:
        return 0

    # Per-session escape hatch, and the reason the hook can be installed into
    # ~/.claude/settings.json at all. Hooks merge additively across settings
    # scopes and a narrower scope cannot un-register a broader one, so settings
    # alone cannot exempt one session -- but an environment variable is
    # per-process, which is finer-grained than any settings file. It is what
    # lets an unpaced supervisor run in the same folder as a paced worker.
    #
    # Any non-empty value exempts: NICECLAUDE_OFF=0 meaning "on" would be a
    # trap, and this is the one setting whose whole job is to be unmistakable.
    #
    # Checked after the stdin read rather than before it, so the caller's write
    # always lands somewhere and cannot fail on a closed pipe.
    if os.environ.get("NICECLAUDE_OFF"):
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
        brake_start, why = run(cwd, payload.get('hook_event_name'), payload)
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
