"""Tests for the parser, the model/path matching, and the pace-line decision.

Nothing here touches the network, the real clock, or the user's data directory:
`parse_usage` and `decide` both take an explicit `now`, which is the whole
reason they are separable from the polling loop.
"""

import json
import os
from datetime import datetime, timezone

import pytest

from niceclaude import cli
from niceclaude import hook
from niceclaude import plot
from niceclaude._shared import (NEAR_STALE, RESET_JITTER, bucket_pace,
                                model_matches, norm_path)

# A fixed instant, so reset-clause parsing (which has to infer the year) is
# deterministic. Chosen a few hours before the sample reset times below.
NOW = datetime(2026, 8, 14, 12, 0, 0, tzinfo=timezone.utc)


def epoch(*args):
    return int(datetime(*args, tzinfo=timezone.utc).timestamp())


# --- cli.parse_usage ---------------------------------------------------------

def test_normal_session_line():
    buckets, unparsed, info = cli.parse_usage(
        "Current session: 11% used \u00b7 resets Aug 14, 8:10pm (UTC)", NOW)
    assert unparsed == []
    assert list(buckets) == ["session"]
    b = buckets["session"]
    assert b["window"] == "session"
    assert b["label"] is None
    assert b["pct"] == 11.0
    assert b["pct_approx"] is False
    assert b["resets_raw"] == "Aug 14, 8:10pm"
    assert b["resets_epoch"] == epoch(2026, 8, 14, 20, 10)
    assert b["tz"] == "UTC"
    assert b["window_seconds"] == 5 * 3600


def test_missing_reset_clause_is_not_an_anomaly():
    """Seen right after a window rolls: the bucket is real, the clause is not."""
    buckets, unparsed, info = cli.parse_usage("Current session: 0% used", NOW)
    assert unparsed == []          # the regression: this used to be reported
    assert info == []              # and it is a bucket, not advisory text
    b = buckets["session"]
    assert b["pct"] == 0.0
    assert b["resets_epoch"] is None
    assert b["resets_raw"] is None
    assert b["window_seconds"] == 5 * 3600


ADVISORY_LINES = [
    "What's contributing to your limits usage?",
    "Last 24h \u00b7 48 requests \u00b7 5 sessions",
    "Top subagents: general-purpose 3%",
    # Mentions "rate limits", which must not trip the SUSPICIOUS check or an
    # account on overage billing reports a false anomaly continuously.
    "You are currently using your overages, which are billed separately from "
    "your subscription's rate limits.",
]


@pytest.mark.parametrize("line", ADVISORY_LINES)
def test_advisory_lines_are_info_not_unparsed(line):
    assert cli.classify(line) == "info"
    buckets, unparsed, info = cli.parse_usage(line, NOW)
    assert unparsed == []
    assert info == [line]
    assert buckets == {}


def test_advisory_block_alongside_buckets():
    raw = (
        "You are currently using your overages, which are billed separately "
        "from your subscription's rate limits.\n"
        "Current session: 11% used \u00b7 resets Aug 14, 8:10pm (UTC)\n"
        "Current week (all models): 14% used \u00b7 resets Aug 16, 12am (UTC)\n"
        "\n"
        "What's contributing to your limits usage?\n"
        "Last 24h \u00b7 48 requests \u00b7 5 sessions\n"
        "Top subagents: general-purpose 3%\n"
    )
    buckets, unparsed, info = cli.parse_usage(raw, NOW)
    assert unparsed == []
    assert sorted(buckets) == ["session", "week:all models"]
    assert len(info) == 4
    assert buckets["week:all models"]["resets_epoch"] == epoch(2026, 8, 16, 0, 0)


@pytest.mark.parametrize("line,key,label,pct", [
    ("Current week (Sonnet only): 47% used", "week:Sonnet only", "Sonnet only", 47.0),
    ("Current week (Fable): 3% used \u00b7 resets Aug 16, 12am (UTC)",
     "week:Fable", "Fable", 3.0),
    ("Current week (all models): 100% used \u00b7 resets Aug 16, 12am (UTC)",
     "week:all models", "all models", 100.0),
    ("Current session: 100% used \u00b7 resets Aug 14, 8:10pm (UTC)",
     "session", None, 100.0),
])
def test_labelled_and_saturated_buckets(line, key, label, pct):
    buckets, unparsed, info = cli.parse_usage(line, NOW)
    assert unparsed == []
    assert list(buckets) == [key]
    assert buckets[key]["label"] == label
    assert buckets[key]["pct"] == pct


@pytest.mark.parametrize("line", [
    "Session limit reached. Try again after 9pm.",
    "Your weekly limit reached for Opus.",
])
def test_unknown_limit_lines_are_reported(line):
    assert cli.classify(line) == "suspicious"
    buckets, unparsed, info = cli.parse_usage(line, NOW)
    assert unparsed == [line]
    assert info == []


# --- _shared.model_matches ---------------------------------------------------

@pytest.mark.parametrize("key,model,expected", [
    ("week:Sonnet only", "sonnet", True),
    ("week:Sonnet only", "Sonnet", True),      # caller may not have lowercased
    ("week:Fable", "fable", True),
    ("week:all models", "opus", False),        # shared bucket, never model-scoped
    ("week:all models", "sonnet", False),
    ("week:all models", "models", False),
    ("week:Fable", "opus", False),
    ("session", "opus", False),                # session is not model-scoped
    ("week:Sonnet only", "", False),
    ("week:Sonnet only", None, False),
])
def test_model_matches(key, model, expected):
    assert model_matches(key, model) is expected


def test_model_matches_is_whole_word_not_substring():
    # "son" is a substring of "sonnet" but not a model.
    assert model_matches("week:Sonnet only", "son") is False


# --- _shared.norm_path / hook.resolve ----------------------------------------

def test_norm_path_strips_trailing_separator_and_dots(tmp_path):
    base = str(tmp_path)
    assert norm_path(base + os.sep) == norm_path(base)
    assert norm_path(os.path.join(base, "a", "..", "a")) == norm_path(
        os.path.join(base, "a"))


def test_norm_path_expands_user():
    assert norm_path("~") == norm_path(os.path.expanduser("~"))


@pytest.fixture
def tree(tmp_path):
    """Real directories, so realpath in norm_path has something to resolve."""
    for rel in ("a", "a/b", "a/b/c", "a/ba", "a/bar"):
        (tmp_path / rel).mkdir(parents=True, exist_ok=True)
    return tmp_path


def _policy(tree, *rules):
    return {"paths": {str(tree / r): entry for r, entry in rules}}


def test_resolve_exact_match(tree):
    pol = _policy(tree, ("a", {"paced": True}))
    key, entry = hook.resolve(pol, norm_path(str(tree / "a")))
    assert key == norm_path(str(tree / "a"))
    assert entry == {"paced": True}


def test_resolve_longest_prefix_wins(tree):
    pol = _policy(tree,
                  ("a", {"paced": True, "m0": 5}),
                  ("a/b", {"paced": True, "m0": 20}))
    key, entry = hook.resolve(pol, norm_path(str(tree / "a" / "b" / "c")))
    assert key == norm_path(str(tree / "a" / "b"))
    assert entry["m0"] == 20


def test_deeper_rule_can_turn_pacing_off(tree):
    pol = _policy(tree, ("a", {"paced": True}), ("a/b", {"paced": False}))
    key, entry = hook.resolve(pol, norm_path(str(tree / "a" / "b" / "c")))
    assert entry["paced"] is False


def test_subfolder_inherits_shallow_rule(tree):
    pol = _policy(tree, ("a", {"paced": True}))
    key, entry = hook.resolve(pol, norm_path(str(tree / "a" / "b" / "c")))
    assert key == norm_path(str(tree / "a"))


def test_sibling_prefix_does_not_match(tree):
    """/a/bar must not match a rule for /a/ba -- component-wise, not string."""
    pol = _policy(tree, ("a/ba", {"paced": True}))
    assert hook.resolve(pol, norm_path(str(tree / "a" / "bar"))) is None


def test_no_rule_at_all(tree):
    pol = _policy(tree, ("a/b", {"paced": True}))
    assert hook.resolve(pol, norm_path(str(tree / "a" / "ba"))) is None
    assert hook.resolve({}, norm_path(str(tree / "a"))) is None


def _anchor(path):
    """The filesystem root containing `path`: "/" on POSIX, "C:\\" on Windows.

    Derived from the path rather than hardcoded, so the drive-root case is the
    one actually exercised when this suite runs on Windows.
    """
    drive, _ = os.path.splitdrive(norm_path(str(path)))
    return drive + os.sep


def test_root_rule_is_a_catch_all(tree):
    """A rule on the root paces everything beneath it, not just the root.

    The root is the one normalized path that already ends in a separator, so
    appending another built "//" and matched nothing below it.
    """
    root = _anchor(tree)
    pol = {"paths": {root: {"paced": True}}}
    key, entry = hook.resolve(pol, norm_path(str(tree / "a" / "b" / "c")))
    assert key == norm_path(root)
    assert entry == {"paced": True}


def test_root_rule_still_matches_the_root_itself(tree):
    root = _anchor(tree)
    pol = {"paths": {root: {"paced": True}}}
    assert hook.resolve(pol, norm_path(root))[1] == {"paced": True}


def test_deeper_rule_beats_the_root_rule(tree):
    """The catch-all must remain the shallowest rule, so carve-outs still win."""
    root = _anchor(tree)
    pol = {"paths": {root: {"paced": True},
                     str(tree / "a"): {"paced": False}}}
    key, entry = hook.resolve(pol, norm_path(str(tree / "a" / "b")))
    assert key == norm_path(str(tree / "a"))
    assert entry["paced"] is False


def test_cli_resolve_agrees_with_hook_on_a_root_rule(tree):
    """`status` reads policy through its own resolver; the two must not drift."""
    root = _anchor(tree)
    pol = {"paths": {root: {"paced": True}}}
    target = str(tree / "a" / "b")
    matched, entry = cli.resolve(pol, target)
    assert norm_path(matched) == norm_path(root)
    assert entry == hook.resolve(pol, norm_path(target))[1]


def test_resolve_skips_unusable_keys(tree):
    pol = {"paths": {str(tree / "a"): {"paced": True}, "\0bad": {"paced": True}}}
    key, entry = hook.resolve(pol, norm_path(str(tree / "a")))
    assert entry == {"paced": True}


# --- hook.decide -------------------------------------------------------------

SESSION_WINDOW = 5 * 3600
WEEK_WINDOW = 7 * 86400
RESETS = 1_760_000_000            # arbitrary fixed epoch; never the real clock
START = RESETS - SESSION_WINDOW
HALFWAY = START + SESSION_WINDOW / 2      # f_t = 0.5
# defaults m0=5, m1=8 -> span 87 -> allowed(0.5) = 48.5
ALLOWED_AT_HALFWAY = 48.5


@pytest.fixture
def cwd(tmp_path):
    d = tmp_path / "work"
    d.mkdir()
    return norm_path(str(d))


def policy_for(cwd, entry=None, enabled=True, defaults=None):
    return {
        "global": {"enabled": enabled},
        "defaults": defaults if defaults is not None else {"m0": 5, "m1": 8,
                                                           "chunk": 15},
        "paths": {cwd: entry if entry is not None else {"paced": True,
                                                        "model": "opus"}},
    }


def session_bucket(pct, resets=RESETS, window=SESSION_WINDOW):
    return {"pct": pct, "resets_epoch": resets, "window_seconds": window,
            "label": None}


def state_for(buckets, now=HALFWAY, age=10):
    return {"ts_epoch": now - age, "buckets": buckets}


def test_under_the_line_is_not_braked(cwd):
    d = hook.decide(policy_for(cwd), state_for({"session": session_bucket(10)}),
                    cwd, HALFWAY)
    assert d["paced"] is True
    assert d["braked"] is False
    assert d["blind"] is False
    assert d["chunk"] == 15


def test_exactly_at_the_line_is_not_braked(cwd):
    # pess = pct + 1 must be strictly above the line to brake. Band off, so the
    # brake line is the only line and this is its edge; the throttle line's own
    # edges are in test_band.py.
    pct = ALLOWED_AT_HALFWAY - 1
    d = hook.decide(policy_for(cwd, defaults={"m0": 5, "m1": 8, "chunk": 15,
                                              "band": 0}),
                    state_for({"session": session_bucket(pct)}),
                    cwd, HALFWAY)
    assert d["braked"] is False


def test_over_the_line_brakes_and_wakes_before_the_reset(cwd):
    d = hook.decide(policy_for(cwd), state_for({"session": session_bucket(90)}),
                    cwd, HALFWAY)
    assert d["paced"] is True
    assert d["braked"] is True
    assert d["blind"] is False
    assert HALFWAY < d["wake_at"] <= RESETS
    assert "session 90% over line" in d["reason"]


def test_wake_at_is_clamped_to_the_reset(cwd):
    """At 100% used the line can never rise to meet us, so the reset is the cap."""
    d = hook.decide(policy_for(cwd), state_for({"session": session_bucket(100)}),
                    cwd, HALFWAY)
    assert d["braked"] is True
    assert d["wake_at"] == RESETS


def test_wake_at_solves_the_pace_line(cwd):
    d = hook.decide(policy_for(cwd), state_for({"session": session_bucket(90)}),
                    cwd, HALFWAY)
    # allowed(f) = 5 + f*87 == 91  ->  f = 86/87
    expected = START + (86 / 87) * SESSION_WINDOW
    assert d["wake_at"] == pytest.approx(expected)


def test_global_kill_switch_false_is_honoured(cwd):
    """Regression: the jq `//` operator fell back on false as well as null,
    so an explicitly disabled pacer stayed enabled."""
    d = hook.decide(policy_for(cwd, enabled=False),
                    state_for({"session": session_bucket(99)}), cwd, HALFWAY)
    assert d["paced"] is False
    assert d == {"paced": False}


def test_unpaced_folder_is_not_paced(cwd):
    d = hook.decide(policy_for(cwd, entry={"paced": False}),
                    state_for({"session": session_bucket(99)}), cwd, HALFWAY)
    assert d["paced"] is False


@pytest.mark.parametrize("entry,braked", [
    # m0 explicitly 0 means 0: pess (0+1) is above the floor, so this brakes.
    ({"paced": True, "model": "opus", "m0": 0}, True),
    # ...whereas the default floor of 5 lets a freshly rolled window through.
    ({"paced": True, "model": "opus"}, False),
])
def test_m0_zero_is_not_defaulted(cwd, entry, braked):
    """Regression: the same `//` truthiness trap turned a configured m0 of 0
    into the default 5, quietly handing every window a free grubstake."""
    state = state_for({"session": {"pct": 0, "resets_epoch": None,
                                   "window_seconds": SESSION_WINDOW,
                                   "label": None}})
    d = hook.decide(policy_for(cwd, entry=entry), state, cwd, HALFWAY)
    assert d["paced"] is True
    assert d.get("braked", False) is braked


@pytest.mark.parametrize("pct,braked", [
    (0, False),     # just rolled: pess 1 is under the m0 floor of 5
    (4, False),     # pess 5 is not *above* 5
    (50, True),     # well above the floor
    (99, True),
])
def test_bucket_without_a_reset_is_judged_against_m0(cwd, pct, braked):
    state = state_for({"session": {"pct": pct, "resets_epoch": None,
                                   "window_seconds": SESSION_WINDOW,
                                   "label": None}})
    d = hook.decide(policy_for(cwd), state, cwd, HALFWAY)
    assert d.get("braked", False) is braked
    if braked:
        assert "over floor 5%" in d["reason"]


def test_degraded_under_the_line_brakes_blind(cwd):
    """Stale data can justify braking but never allowing."""
    d = hook.decide(policy_for(cwd), state_for({"session": session_bucket(10)}),
                    cwd, HALFWAY, degraded=True)
    assert d["braked"] is True
    assert d["blind"] is True
    assert "BLIND" in d["reason"]
    assert d["wake_at"] == HALFWAY + 15


def test_degraded_over_the_line_is_confident(cwd):
    """Usage only rises, so an old reading that is already hot is a floor."""
    d = hook.decide(policy_for(cwd), state_for({"session": session_bucket(90)}),
                    cwd, HALFWAY, degraded=True)
    assert d["braked"] is True
    assert d["blind"] is False
    assert "BLIND" not in d["reason"]


def test_unmatched_model_bucket_is_ignored_even_at_99(cwd):
    state = state_for({
        "session": session_bucket(10),
        "week:Fable": {"pct": 99, "resets_epoch": RESETS,
                       "window_seconds": WEEK_WINDOW, "label": "Fable"},
    })
    d = hook.decide(policy_for(cwd, entry={"paced": True, "model": "opus"}),
                    state, cwd, HALFWAY)
    assert d["braked"] is False


def test_matched_model_bucket_is_enforced(cwd):
    """Same state as above; only the declared model differs."""
    state = state_for({
        "session": session_bucket(10),
        "week:Fable": {"pct": 99, "resets_epoch": RESETS,
                       "window_seconds": WEEK_WINDOW, "label": "Fable"},
    })
    d = hook.decide(policy_for(cwd, entry={"paced": True, "model": "fable"}),
                    state, cwd, HALFWAY)
    assert d["braked"] is True
    assert "week:Fable" in d["reason"]


def test_no_usable_buckets_brakes_blind(cwd):
    state = state_for({"week:Fable": {"pct": 1, "resets_epoch": RESETS,
                                      "window_seconds": WEEK_WINDOW,
                                      "label": "Fable"}})
    d = hook.decide(policy_for(cwd, entry={"paced": True, "model": "opus"}),
                    state, cwd, HALFWAY)
    assert d["braked"] is True
    assert d["blind"] is True


def test_shared_weekly_bucket_is_always_enforced(cwd):
    state = state_for({
        "session": session_bucket(10),
        "week:all models": {"pct": 99, "resets_epoch": RESETS,
                            "window_seconds": WEEK_WINDOW,
                            "label": "all models"},
    })
    d = hook.decide(policy_for(cwd), state, cwd, HALFWAY)
    assert d["braked"] is True
    assert "week:all models" in d["reason"]


# --- a reading whose window has rolled is void, not a position ---------------
#
# `bucket_pace` used to compute `elapsed = (now - start) / window` with no
# upper bound, so a session window read 52h late reported `elapsed` 10.56, a
# brake line at 853.9% and a throttle line at 839.9% -- and then classified
# `free`, because the lines had floated above any percentage a bucket can
# hold. The verdict that followed happened to be right (both buckets free plus
# degraded falls through to the blind branch), but nothing in that path had
# reasoned about the rolled window at all.
#
# A rolled window makes its reading VOID. It is not a lower bound on current
# usage, because the window was zeroed, and it is not evidence of headroom,
# because the window now running has never been observed. Clamping `elapsed`
# to 1.0 would make the display sane and the braking wrong -- a stale 95%
# reading would sit in band/over against a 99% line and brake on a window that
# reset days ago -- so the reading has to be voided, never re-anchored.
# harness/expired-window-plan.md holds the decisions cited as E1-E10 below.

EXPIRED_NOW = RESETS + 3600          # an hour past the session window's reset
JITTER_EDGE = RESETS + RESET_JITTER  # the last instant that is still live


def week_bucket(pct, resets):
    return {"pct": pct, "resets_epoch": resets, "window_seconds": WEEK_WINDOW,
            "label": "all models"}


# --- E1/E2: where a window expires, and what `elapsed` may be ----------------

def test_the_jitter_slack_is_the_boundary_and_it_is_strict():
    """E1, and the only place the operator itself is pinned.

    The server rounds the reset times it prints -- 8:09pm and 8:10pm are the
    same instant -- so a live window in its final minute reads as already past
    its own reset. Without the slack every window end would spend two minutes
    voiding perfectly good readings, which costs most where it would be
    noticed least: an under-the-line reading would become a blind hard hold.

    The slack is also what makes `>` versus `>=` decidable only HERE. At `now
    == resets` neither operator expires anything, because the slack swallows
    both; the two differ at the edge below and nowhere else.
    """
    live = bucket_pace(session_bucket(50), JITTER_EDGE, 5, 8, 7)
    assert live["expired"] is False
    dead = bucket_pace(session_bucket(50), JITTER_EDGE + 1, 5, 8, 7)
    assert dead["expired"] is True


@pytest.mark.parametrize("now", [
    RESETS - 5 * 3600,          # the window start, f_t = 0 exactly
    RESETS - 1,
    RESETS,                     # the printed reset
    RESETS + 1,
    RESETS + RESET_JITTER // 2,
    JITTER_EDGE,                # the last live instant
])
def test_elapsed_is_always_a_fraction_of_a_window(now):
    """E2. Inside the slack the line is clamped rather than extrapolated --
    "the line is at its final height" is true for that last minute, "the line
    is at 103%" never is -- and past the slack `elapsed` is None rather than a
    number. That is what retires the 1055.5% figure `status` printed.

    This spans the whole window and its slack, which is every `now` that
    reaches the arithmetic at all: before the start is its own branch (see
    the pre-start test below) and after the slack is expiry.
    """
    p = bucket_pace(session_bucket(50), now, 5, 8, 7)
    assert p["expired"] is False
    assert 0.0 <= p["elapsed"] <= 1.0
    # ...and the lines that follow from it stay on the board.
    assert 5 <= p["low"] <= p["allowed"] <= 92


@pytest.mark.parametrize("hours_out", [6, 20])
def test_a_window_that_has_not_begun_yet_is_judged_against_the_floor(cwd,
                                                                     hours_out):
    """A reset clause further out than one window length puts `now` BEFORE the
    window it implies. Clock skew does it; so does a reparse landing a session
    reset hours ahead.

    It used to fall through to the solve, where it was wrong twice. At 20h out
    with pct 50 the lines ran negative -- elapsed -3.0, a brake line at -256%
    that nothing can ever be under. And the solve returned a wake 18.05h away
    which `decide` read as REAL, so the hold was CONFIDENT: blind_delay cannot
    release one of those and max_delay is off by default, leaving the 48h hook
    timeout as the only ceiling. That is the stall-forever class the user rule
    forbids outright.

    Clamping the lines alone would have hidden it rather than fixed it -- the
    row would read `over line 5.0%` at `0.0% elapsed`, indistinguishable from
    an ordinary start-of-window brake. So the whole shape is the floor shape,
    which is what hands the hold to THE BLIND RULE and to blind_delay.
    """
    b = session_bucket(50, resets=RESETS + hours_out * 3600)
    p = bucket_pace(b, RESETS, 5, 8, 7)
    # No wake to aim at is the load-bearing half: that is what makes it blind.
    assert p["wake"] is None
    assert p["wait"] is None
    assert p["expired"] is False        # not rolled -- not started
    # Identical to the no-reset-clause shape, key for key and value for value.
    # They are literally one return statement, and must stay so.
    assert p == bucket_pace({"pct": 50, "resets_epoch": None,
                             "window_seconds": None, "label": None},
                            RESETS, 5, 8, 7)
    d = hook.decide(policy_for(cwd), state_for({"session": b}, now=RESETS),
                    cwd, RESETS)
    assert d["blind"] is True
    assert d["wake_at"] == RESETS + 15          # the chunk, not 18h away
    assert "over floor 5%" in d["reason"]
    assert d["blind_delay"] is not None         # ...and a ceiling exists


@pytest.mark.parametrize("window", [0, -5])
def test_a_non_positive_window_never_reaches_the_division(window):
    """E13. Section 1.4's hazard reached directly, and the reason this test's
    point is that it RETURNS rather than what it returns.

    window_seconds comes back out of state.json unvalidated. A 0 divided by
    zero, which hook.main catches and logs `-- failing open`, leaving the
    folder silently unpaced -- worse than any hold and worse than any
    release. A -5 did not even raise: it put `start` AFTER `resets`, so the
    pre-start test could not catch it either, and it reported elapsed -9.0
    against a brake line of -778% while holding `over` with a wait of zero.

    The floor shape rather than the void shape, because garbage in
    window_seconds does not make the pct garbage -- only the window position
    is destroyed, and the floor is already the answer to "fresh pct, no
    usable position".

    `now` is chosen to REACH the division, which is the whole difficulty: a
    non-positive window puts `start` at or after `resets`, so at any earlier
    instant the pre-start branch swallows both cases and this test passes
    with the guard deleted. RESETS + 50 is past `start` for both (0 puts it
    at `resets`, -5 at `resets + 5`) and still inside the jitter slack, so
    expiry does not take them either. It reproduces both measurements
    exactly: 0 raised ZeroDivisionError, -5 gave elapsed -9.0 and a brake
    line of -778%.
    """
    now = RESETS + 50
    p = bucket_pace({"pct": 50, "resets_epoch": RESETS,
                     "window_seconds": window, "label": None},
                    now, 5, 8, 7)
    assert p == bucket_pace({"pct": 50, "resets_epoch": None,
                             "window_seconds": None, "label": None},
                            now, 5, 8, 7)


@pytest.mark.parametrize("field, value", [
    ("window_seconds", "18000"),
    ("resets_epoch", "x"),
    ("pct", "50"),
])
def test_non_numeric_state_fields_return_rather_than_raise(field, value):
    """E13's guard extended to the corruption its neighbour above cannot see:
    section 1.4's hazard again, by type rather than by value.

    state.json is hand-editable, and a quoted number is the obvious slip --
    the same one coerce_num exists for in policy.json. Each of these three
    raised TypeError before the type test existed (a non-numeric window or
    resets at the `resets - window` subtraction, which ran AHEAD of the old
    `window <= 0` test; a non-numeric pct at `pess = pct + 1`), and every
    raise lands in hook.main's catch-all, which logs `-- failing open` and
    leaves the folder silently unpaced on every call.

    The shapes split the way E13 argues: garbage in the window POSITION does
    not make the pct garbage, so those two take the same floor shape a
    non-positive window takes. Garbage in the pct is the opposite -- the
    position may be fine, but there is no percentage to judge -- and that is
    this function's documented None, which `decide` already files as an
    unusable (blind) hot entry.

    `now` sits mid-window so a healthy bucket would be live, proving it is
    the corrupt field alone that diverts the return.
    """
    bucket = {"pct": 50, "resets_epoch": RESETS,
              "window_seconds": SESSION_WINDOW, "label": None}
    bucket[field] = value
    p = bucket_pace(bucket, HALFWAY, 5, 8, 7)  # must return, never raise
    if field == "pct":
        assert p is None
    else:
        assert p == bucket_pace({"pct": 50, "resets_epoch": None,
                                 "window_seconds": None, "label": None},
                                HALFWAY, 5, 8, 7)
        assert p["expired"] is False


def test_the_pre_start_hold_is_released_by_blind_delay(held, cwd):
    """The end of the same story, on the virtual clock: the hold that used to
    run to the 48h hook timeout now ends at blind_delay."""
    b = session_bucket(50, resets=RESETS + 20 * 3600)
    reason, _refreshes, clock = held(
        {"session": b}, RESETS,
        entry={"paced": True, "model": "opus", "blind_delay": 60})
    assert reason == "blind-release"
    assert clock.t - RESETS == 60


def test_inside_the_slack_the_line_sits_at_its_final_height():
    """The clamp is to the END of the window, not to anything lower: m0 plus
    the whole span, which with m0 5 and m1 8 is 100 - m1."""
    p = bucket_pace(session_bucket(50), JITTER_EDGE, 5, 8, 7)
    assert p["elapsed"] == 1.0
    assert p["allowed"] == pytest.approx(92.0)
    assert p["low"] == pytest.approx(85.0)


# --- E3: the expired shape is total, and it fails toward holding -------------

def test_the_expired_shape_carries_the_same_keys_as_a_live_one():
    """E3, and it guards the worst failure available here. hook.main catches
    every exception and logs `-- failing open`, so a partial shape that
    crashed some forgotten consumer would not void one reading -- it would
    stop pacing the folder entirely, which is worse than either braking or
    running free. The numbers are numbers for the same reason: a formatter
    that reaches for `allowed` must find something it can format.
    """
    live = bucket_pace(session_bucket(50), HALFWAY, 5, 8, 7)
    floor = bucket_pace({"pct": 50, "resets_epoch": None,
                         "window_seconds": None, "label": None},
                        HALFWAY, 5, 8, 7)
    dead = bucket_pace(session_bucket(50), EXPIRED_NOW, 5, 8, 7)
    assert set(dead) == set(live) == set(floor)
    # The formatting `status` does on these, verbatim: a None in either slot
    # raises TypeError here, which is the crash the totality exists to stop.
    rendered = f"{dead['allowed']:5.1f}/{dead['low']:5.1f} {dead['pct']:>3.0f}"
    assert rendered == "  5.0/  5.0  50"


def test_the_expired_shape_fails_toward_holding():
    """E3. A consumer that has never heard of `expired` must stop, not
    proceed, and must find no release to aim at."""
    dead = bucket_pace(session_bucket(50), EXPIRED_NOW, 5, 8, 7)
    assert dead["expired"] is True
    assert dead["region"] == "over"
    assert dead["over"] is True
    assert dead["wait"] is None
    assert dead["wake"] is None
    assert dead["elapsed"] is None
    # At the m0 floor purely so a formatter cannot raise. NOT a line that was
    # ever in force -- `status` branches on `expired` before it prints either.
    assert dead["allowed"] == 5
    assert dead["low"] == 5


@pytest.mark.parametrize("pct", [0, 3, 50, 95, 100])
def test_a_rolled_window_is_void_at_every_percentage(pct):
    """Void means void: a 3% reading is no more evidence of headroom than a
    95% one is evidence of usage, because the window both describe is gone."""
    assert bucket_pace(session_bucket(pct), EXPIRED_NOW, 5, 8, 7)["expired"]


# --- E4: `decide` says what actually happened --------------------------------

def test_decide_names_the_rolled_window_in_its_own_words(cwd):
    """E4. Left to the wake-less arm this would log "session 95% over floor
    5%", which is false -- the bucket is not over any floor, its window is
    gone -- and hook.log is read by eye."""
    d = hook.decide(policy_for(cwd),
                    state_for({"session": session_bucket(95)}, now=EXPIRED_NOW),
                    cwd, EXPIRED_NOW)
    assert d["reason"] == "session: window reset since this reading"
    assert "over floor" not in d["reason"]


def test_an_expired_bucket_holds_blind_and_re_checks_on_the_chunk(cwd):
    """A wake-less hot entry, which THE BLIND RULE already classifies blind.
    No new policy: "cannot see" is what this codebase answers an empty
    snapshot with, and a void reading is strictly less informative than a
    degraded one."""
    d = hook.decide(policy_for(cwd),
                    state_for({"session": session_bucket(95)}, now=EXPIRED_NOW),
                    cwd, EXPIRED_NOW)
    assert d["braked"] is True
    assert d["hold"] == "hard"
    assert d["blind"] is True
    assert d["region"] == "over"
    assert d["wake_at"] == EXPIRED_NOW + 15


@pytest.mark.parametrize("now", [
    RESETS + 30,             # inside the jitter slack: clamped, not voided
    RESETS + RESET_JITTER,   # the last live instant, still clamped
    RESETS + 600,            # past the slack: voided
])
def test_a_rolled_window_never_holds_against_a_wake_already_past(cwd, now):
    """The live defect this change was written for (plan section 1.3).

    At m0 5 / m1 8 / band 7 a 90% session bucket read 600s after its reset
    extrapolated to a brake line of 94.9% and a throttle line of 87.9%, which
    put pess 91 squarely in the BAND -- and its wake solved to the reset
    itself, 600s in the PAST. The soft-band branch has no past-wake guard of
    its own, so `run` took a soft hold whose `wake_at` was behind it and
    napped the 1s floor through the whole band_delay rather than the 12 naps
    one chunk apart the hold is worth. Reachable with no `degraded` anywhere:
    a sample 100s old with a roll 30s ago is well inside MAX_STALE.

    Both sides of the boundary, because expiry alone does not cover it. Past
    the slack the reading is voided and the hold is a blind hard one. INSIDE
    the slack the reading is deliberately kept -- the window may well still be
    running -- so it takes E2's clamp, still classifies `band`, and still
    solves its wake to the reset behind us. That is the case `decide`'s soft
    return has to floor at `now + chunk` itself.
    """
    d = hook.decide(policy_for(cwd),
                    state_for({"session": session_bucket(90)}, now=now),
                    cwd, now)
    assert d["braked"] is True
    assert d["wake_at"] > now            # never an instant already past


def test_a_near_future_band_wake_is_not_rounded_up_to_a_chunk(cwd):
    """The other side of that floor, and why it is CONDITIONAL.

    A band wake a few seconds out is ordinary, not pathological: the session
    line rises about a point every 207s, so a throttle crossing inside the
    default 15s chunk is common. Flooring unconditionally would round every
    one of those up to a whole chunk and release late by up to `chunk` -- a
    plausible-looking wrong answer, where the unguarded code at least timed
    these exactly. It also feeds `status`, whose band branch prints this wake
    as "full speed resumes in ..." with no guard of its own.

    So only a wake already BEHIND us is replaced, mirroring the hot arm's
    `real`.
    """
    # pess 42 meets the throttle line (band 7 under m0 + f_t * span) here.
    crossing = START + ((42 + 7 - 5) / 87) * SESSION_WINDOW
    now = crossing - 5                     # five seconds short of it
    p = bucket_pace(session_bucket(41), now, 5, 8, 7)
    assert p["region"] == "band"
    assert p["wake"] == pytest.approx(crossing)
    d = hook.decide(policy_for(cwd),
                    state_for({"session": session_bucket(41)}, now=now),
                    cwd, now)
    assert d["hold"] == "soft"
    assert d["wake_at"] == pytest.approx(crossing)   # NOT now + chunk
    assert d["wake_at"] - now == pytest.approx(5.0)


def test_the_band_to_free_slide_past_the_boundary_is_gone(cwd):
    """The other half of 1.3: as the extrapolated line climbed, the same
    bucket slid band -> free and the hold vanished altogether. A 95% session
    reading an hour past its reset used to classify `free` against a line at
    109.4%."""
    assert bucket_pace(session_bucket(95), EXPIRED_NOW, 5, 8, 7)["region"] != "free"
    d = hook.decide(policy_for(cwd),
                    state_for({"session": session_bucket(95)}, now=EXPIRED_NOW),
                    cwd, EXPIRED_NOW)
    assert d["braked"] is True


# --- E9: expiry is per bucket, and a live sibling still governs --------------

def test_a_live_weekly_sibling_keeps_an_expired_session_hold_confident(cwd):
    """E9. The 52h-stale snapshot that started all this had a session window
    long gone and weekly windows genuinely at 99.7% of theirs. The weekly
    reading has NOT expired and is still a valid lower bound -- usage only
    rises within a window -- so the hold it demands is a confident one. The
    expired sibling must not drag it blind: THE BLIND RULE asks for one
    supported wake anywhere, however wake-less the rest.
    """
    week_resets = EXPIRED_NOW + 2 * 86400
    state = state_for({"session": session_bucket(3),
                       "week:all models": week_bucket(99, week_resets)},
                      now=EXPIRED_NOW)
    d = hook.decide(policy_for(cwd), state, cwd, EXPIRED_NOW)
    assert d["braked"] is True
    assert d["blind"] is False                 # confident, not blind
    assert d["wake_at"] == week_resets         # the live sibling's own wake
    assert "session: window reset since this reading" in d["reason"]
    assert "week:all models 99% over line" in d["reason"]


def test_the_expired_verdict_flag_is_set_by_any_enforced_bucket(cwd):
    """E6. `run` reads this to force one refresh, so it has to be true when
    ANY enforced bucket is void, not only when every one of them is."""
    week_resets = EXPIRED_NOW + 2 * 86400
    both = hook.decide(policy_for(cwd),
                       state_for({"session": session_bucket(3),
                                  "week:all models": week_bucket(99, week_resets)},
                                 now=EXPIRED_NOW),
                       cwd, EXPIRED_NOW)
    assert both["expired"] is True
    live = hook.decide(policy_for(cwd),
                       state_for({"session": session_bucket(90)}, now=HALFWAY),
                       cwd, HALFWAY)
    assert live["expired"] is False


# A session window still running at EXPIRED_NOW, so the two buckets below
# disagree about whether anything has rolled and the `enforce` set is what
# settles it.
LIVE_SESSION_RESETS = RESETS + 4 * 3600


@pytest.mark.parametrize("enforce,flagged,reason", [
    ("session", False, None),
    ("week", True, "week:all models: window reset since this reading"),
    ("model", True, "week:Opus: window reset since this reading"),
])
def test_only_an_enforced_bucket_can_flag_the_verdict_expired(cwd, enforce,
                                                              flagged, reason):
    """E6/E9. The flag exists to force a refresh for a window this folder
    answers to, so a rolled bucket the rule ignores must not set it --
    refreshing for someone else's window is churn.

    All three arms read the SAME snapshot and all three brake, on different
    buckets, so the flag is the only thing that differs and the assertion
    distinguishes something.

    One arm per limb of the `enforced` filter, which is where this could go
    wrong without anything noticing. The `week` arm also runs a seven-day
    window through the expiry branch rather than the five-hour one every
    other test here voids. The `model` arm covers the limb with its own
    early-return path immediately above the loop -- the one that lets a call
    matching no per-model row run free -- so a rolled row that DOES match has
    to get past it and reach the expiry arm.
    """
    state = state_for({"session": session_bucket(90,
                                                 resets=LIVE_SESSION_RESETS),
                       "week:all models": week_bucket(99, RESETS),
                       "week:Opus": {"pct": 99, "resets_epoch": RESETS,
                                     "window_seconds": WEEK_WINDOW,
                                     "label": "Opus"}},
                      now=EXPIRED_NOW)
    d = hook.decide(policy_for(cwd, entry={"paced": True, "model": "opus",
                                           "enforce": enforce}),
                    state, cwd, EXPIRED_NOW)
    assert d["braked"] is True
    assert d["expired"] is flagged
    if flagged:
        assert d["reason"] == reason
        assert d["blind"] is True
    else:
        # The live session bucket, held on its own line and nobody else's.
        assert "over line" in d["reason"]
        assert "window reset" not in d["reason"]


def test_several_buckets_expiring_at_once_collapse_to_one_blind_hold(cwd):
    """E4/E9. With a daemon down for days every window rolls, so this is the
    ordinary shape of the problem rather than a corner of it.

    Nothing here has a wake, so `max(h[2] for h in hot)` collapses to the one
    value they all carry -- the chunk -- and the hold is blind with no
    supported wake anywhere. The reason names both, in `enforced` order, so
    hook.log says which windows went rather than just that something did.
    """
    state = state_for({"session": session_bucket(95),
                       "week:all models": week_bucket(99, RESETS)},
                      now=EXPIRED_NOW)
    d = hook.decide(policy_for(cwd, entry={"paced": True, "model": "opus",
                                           "enforce": "session,week"}),
                    state, cwd, EXPIRED_NOW)
    assert d["expired"] is True
    assert d["blind"] is True
    assert d["wake_at"] == EXPIRED_NOW + 15
    assert d["reason"] == ("session: window reset since this reading; "
                           "week:all models: window reset since this reading")


# --- E3/E6: `expired` is on every paced verdict, not only the hot one -------

def test_every_paced_verdict_carries_the_expired_key(cwd):
    """E3's own totality argument, applied to the verdict shape.

    `wake_at` and `reason` are facts about a hold and are legitimately absent
    when nothing brakes. `expired` is a property of the SNAPSHOT, and a
    snapshot always has an answer. hook.main catches every exception and fails
    open, so one future consumer reading `d["expired"]` on a branch that
    omitted it would not misreport a verdict -- it would stop pacing the
    folder entirely.

    Walks every paced branch of `decide` EXCEPT the hot one -- free, soft
    band, the latched descent, the degraded blind hold, no-usable-buckets and
    the per-model free path. The hot branch is the only one that can report
    this True, and the expiry tests above are where it is covered.
    """
    free = state_for({"session": session_bucket(10)})
    band = state_for({"session": session_bucket(45)})
    # Over the line, so the per-model arm below says something: its early-out
    # returns braked False, while falling through to the loop would brake on
    # this bucket. With a free bucket both answers look identical.
    hot = state_for({"session": session_bucket(90)})
    verdicts = {
        "free": hook.decide(policy_for(cwd), free, cwd, HALFWAY),
        "soft": hook.decide(policy_for(cwd), band, cwd, HALFWAY),
        "running down": hook.decide(policy_for(cwd), band, cwd, HALFWAY,
                                    hard=True),
        "degraded": hook.decide(policy_for(cwd), free, cwd, HALFWAY,
                                degraded=True),
        "no usable buckets": hook.decide(policy_for(cwd), state_for({}), cwd,
                                         HALFWAY),
        "no per-model row": hook.decide(
            policy_for(cwd, entry={"paced": True, "model": "opus",
                                   "enforce": "model"}),
            hot, cwd, HALFWAY),
    }
    for name, d in verdicts.items():
        assert "expired" in d, f"{name} verdict omits it"
        assert d["expired"] is False, name
    # Guard the guard: these really are the branches claimed, not six copies
    # of one of them.
    assert verdicts["soft"]["hold"] == "soft"
    assert "[running down]" in verdicts["running down"]["reason"]
    assert verdicts["no per-model row"]["braked"] is False
    # ...and the unpaced early-out is deliberately NOT in that set: it is not
    # a verdict, carries no region or chunk either, and every caller tests
    # `paced` first.
    assert hook.decide(policy_for(cwd, entry={"paced": False}), free, cwd,
                       HALFWAY) == {"paced": False}


# --- E10: the `hard` latch needs no change -----------------------------------

def test_a_latched_descent_whose_window_expires_goes_blind_not_free(cwd):
    """E10, pinned rather than merely reasoned about.

    `hard` is the caller's memory that this hold began above the brake line,
    and it is what carries a hold down through the band instead of releasing
    at the brake line on the way past. If the window rolls mid-descent the
    reading backing it is void, so the descent is no longer owed by anything:
    the bucket goes expired -> hot -> blind. It must not go free, and it must
    not stay confident. `supported` counts only BANDED wakes, which an
    expired bucket no longer produces, so the latch cannot manufacture one.
    """
    state = state_for({"session": session_bucket(95)}, now=EXPIRED_NOW)
    latched = hook.decide(policy_for(cwd), state, cwd, EXPIRED_NOW, hard=True)
    assert latched["braked"] is True
    assert latched["hold"] == "hard"
    assert latched["blind"] is True
    # ...and identically to the unlatched verdict: `hard` changes nothing
    # here, which is the claim E10 makes.
    assert latched == hook.decide(policy_for(cwd), state, cwd, EXPIRED_NOW)


def test_a_latched_descent_is_not_unlatched_by_a_live_band_sibling(cwd):
    """The latch's own guarantee survives one bucket going void: a week
    bucket still in the band supports the descent, so the hold stays
    confident, while the expired session still refuses to supply a wake."""
    week_resets = EXPIRED_NOW + 6 * 86400   # f_t ~ 1/7, line ~17.4%
    state = state_for({"session": session_bucket(95),
                       "week:all models": week_bucket(15, week_resets)},
                      now=EXPIRED_NOW)
    d = hook.decide(policy_for(cwd), state, cwd, EXPIRED_NOW, hard=True)
    assert d["braked"] is True
    assert d["hold"] == "hard"
    assert d["blind"] is False          # the band sibling's wake is supported
    # The three above are all true of the pre-change tree too, which judged
    # the void session against an extrapolated line and reached the same
    # verdict by the wrong route. These are what say WHICH branch ran: the
    # session is hot (so the region is over, not band), it is named as
    # rolled rather than as over a line, the hot branch is what returned --
    # not the running-down one -- and the wake is the chunk, because the one
    # hot entry has no wake of its own.
    assert d["region"] == "over"
    assert "session: window reset since this reading" in d["reason"]
    assert "[running down]" not in d["reason"]
    assert d["wake_at"] == EXPIRED_NOW + 15


# --- E5: the plot module, and the synthetic one-second window ----------------

def test_unit_pace_at_the_right_edge_draws_the_final_line_height():
    """E5. _draw_overlay_band samples ft over [0, 1.0] INCLUSIVE, so the
    right-hand edge asks bucket_pace about `now == resets_epoch == 1.0`
    exactly on a synthetic one-second window. Under a bare `now >= resets`
    boundary that is expired, and the band would either plunge to the m0
    floor at its right edge or raise. Under the strict `>` -- with 120s of
    slack against a 1s window besides -- it is live, and takes the clamp.
    """
    allowed, low = plot._unit_pace(1.0, 5, 8, 7)
    assert allowed == pytest.approx(92.0)
    assert low == pytest.approx(85.0)


def test_the_whole_overlay_band_still_draws():
    """The panel samples 1001 points over [0, 1.0]; none of them may raise,
    and the line must rise monotonically to its final height."""
    pairs = [plot._unit_pace(i / 1000.0, 5, 8, 7) for i in range(1001)]
    assert pairs[0][0] == pytest.approx(5.0)
    assert pairs[-1][0] == pytest.approx(92.0)
    assert all(b[0] >= a[0] for a, b in zip(pairs, pairs[1:]))


def _sample(ts, pct, resets):
    return {"ts_epoch": ts, "pct": pct, "resets_epoch": resets,
            "window_seconds": SESSION_WINDOW, "label": None}


def test_pace_draws_inside_the_slack_and_refuses_past_it():
    """E1/E5. A recorded sample whose own timestamp is past the reset clause
    it carried is a lagging clause, and past the slack it is the same void
    reading the hook holds BLIND on -- decide's expired arm brakes it hard,
    bounded by blind_delay -- refusing only to treat it as a position and
    solve a pace line from it. Drawing a flat line at the m0 grubstake there
    would claim a pace line that was never in force."""
    inside = _sample(JITTER_EDGE, 50, RESETS)
    assert plot._pace(inside, 5, 8, 7) == pytest.approx((92.0, 85.0))
    outside = _sample(JITTER_EDGE + 1, 50, RESETS)
    assert plot._pace(outside, 5, 8, 7) is None


def test_pace_still_refuses_a_sample_with_no_reset_clause():
    """Unchanged, and distinct: "never carried a clause" and "carried one its
    own timestamp has outrun" are different facts about a sample, which is
    why _pace tests `expired` explicitly rather than leaning on the None
    elapsed they happen to share."""
    assert plot._pace({"ts_epoch": HALFWAY, "pct": 50, "resets_epoch": None,
                       "window_seconds": None, "label": None}, 5, 8, 7) is None


# --- E3: what `status` prints for a void reading -----------------------------

@pytest.fixture
def status_folder(tmp_path, monkeypatch):
    """A paced folder, a snapshot, and a clock frozen wherever a test wants.

    Modelled on test_band_cli.py's `paced` fixture: `status` has to read the
    same policy and the same state file the hook does, and ask the hook
    itself for the verdict, or it becomes a second opinion -- which is worse
    than no report, because it is believed.
    """
    monkeypatch.setattr(cli, "POLICY_PATH", str(tmp_path / "policy.json"))
    monkeypatch.setattr(cli, "STATE_PATH", str(tmp_path / "state.json"))
    work = tmp_path / "work"
    work.mkdir()
    folder = norm_path(str(work))

    def setup(now, buckets, entry=None):
        monkeypatch.setattr(cli.time, "time", lambda: float(now))
        rule = {"paced": True, "model": "opus", "enforce": "session"}
        rule.update(entry or {})
        cli.save_policy({"global": {"enabled": True},
                         "defaults": {"m0": 5, "m1": 8, "chunk": 15},
                         "paths": {folder: rule}})
        cli.write_atomic(cli.STATE_PATH, json.dumps(
            {"ts_epoch": int(now) - 10, "ok": True, "buckets": buckets}))
        return folder

    return setup


def test_status_reports_a_rolled_window_as_void(status_folder, capsys):
    """E3. The row that produced this plan read

        session   3% used | lines 839.9/853.9% | 1055.5% elapsed | ENFORCED

    on a session snapshot 51h past its reset. Every number in it was an
    extrapolation off the end of a window that no longer existed. The
    replacement says so, and prints neither line: bucket_pace parks both on
    the m0 floor as a crash guard, and showing either would present a line
    that was never in force. The duration is human_delta's, as every other
    duration in this report is, so 51 hours reads as 2d03h.
    """
    now = RESETS + 51 * 3600
    folder = status_folder(now, {"session": session_bucket(3)})
    assert cli.cmd_status(folder) == 0
    out = capsys.readouterr().out
    # The `where` field and nothing else, because that field is the expired
    # branch's entire contribution to the row: at these margins the pre-fix
    # text read `lines 972.4/979.4% | 1120.0% elapsed`, and an equality is the
    # only assertion no extrapolated number can slip past. The digits in the
    # plan's own transcript (839.9/853.9, 1055.5) come from the reporting
    # account's margins, not these, so asserting their absence would prove
    # nothing here.
    #
    # The other columns belong to other tests and are pinned there: the mark
    # in test_model_detection.py and test_exempt.py, the hold wording and the
    # blind-delay rendering several times over in test_blind_delay.py.
    row = next(ln for ln in out.splitlines() if ln.strip().startswith("session"))
    fields = [f.strip() for f in row.split(" | ")]
    assert fields[1] == "window reset 2d03h ago -- reading is void"
    assert "BRAKED, blind -- session: window reset since this reading" in out
    # The verdict is a CONDITIONAL, and on a 10s-old snapshot at that. E6
    # forces a refresh on an expired bucket whatever the file's age, and the
    # likeliest outcome is no hold at all: the post-roll sample carries no
    # reset clause, 3% is under the m0 floor of 5, and the call goes through.
    assert "only actually holds here if that refresh fails." in out


def test_status_renders_a_void_per_call_row_under_detect(status_folder,
                                                        capsys):
    """E3. Under `detect` a per-model row binds some callers and not others,
    so `status` marks it `per call` and re-runs `decide` AS that caller to
    find out whether its hold would be blind. That second call is a separate
    path through the expiry arm, with the caller supplied rather than absent,
    and nothing else here exercises it.
    """
    now = RESETS + 51 * 3600
    folder = status_folder(now,
                           {"week:Fable": {"pct": 99, "resets_epoch": RESETS,
                                           "window_seconds": WEEK_WINDOW,
                                           "label": "Fable"}},
                           entry={"model": "detect", "enforce": "model"})
    assert cli.cmd_status(folder) == 0
    row = next(ln for ln in capsys.readouterr().out.splitlines()
               if "week:Fable" in ln)
    fields = [f.strip() for f in row.split(" | ")]
    assert fields[1] == "window reset 2d03h ago -- reading is void"
    assert fields[2] == "per call"
    # Bounded -- the answer only the re-run with a caller can give: the
    # verdict `status` asked without one never saw this row, so without that
    # re-run the column reports an unbounded hold.
    assert "through after" in fields[3]


def test_status_renders_a_table_where_every_window_has_rolled(status_folder,
                                                              capsys):
    """E3. The daemon-down-for-days case end to end: every window has gone,
    so every row is void and the verdict names all of them. Nothing in the
    table may fall back to reporting a line."""
    now = RESETS + 51 * 3600
    folder = status_folder(now, {"session": session_bucket(95),
                                 "week:all models": week_bucket(99, RESETS)},
                           entry={"enforce": "session,week"})
    assert cli.cmd_status(folder) == 0
    out = capsys.readouterr().out
    rows = [ln for ln in out.splitlines() if "% used |" in ln]
    assert len(rows) == 2
    for row in rows:
        fields = [f.strip() for f in row.split(" | ")]
        assert fields[1] == "window reset 2d03h ago -- reading is void"
    assert "BRAKED, blind" in out


def test_status_does_not_promise_a_crossing_that_cannot_happen(status_folder,
                                                               capsys):
    """E11. In the jitter zone a band bucket's release solves past the window
    end and is clipped back to a `resets` already behind us, and `decide`
    substitutes the next chunk so `run` does not spin at its 1s nap floor.
    That substitute is a nap target, not a release: printing it as "full
    speed resumes in 15s, when the throttle line catches up" is false twice
    over -- the throttle line is not what ends this hold, band_delay is -- and
    it is a worse answer than the bare `0s` it replaced, because it looks
    right.

    The ordinary band case still says it, and is pinned in test_band_cli.py,
    so a guard that fired unconditionally would break there rather than here.
    """
    folder = status_folder(RESETS + 30, {"session": session_bucket(90)},
                           entry={"band_delay": 180})
    assert cli.cmd_status(folder) == 0
    out = capsys.readouterr().out
    assert "throttle line catches up" not in out
    assert "3m00s per call is what releases this" in out


def test_status_says_when_a_window_that_has_not_begun_starts(status_folder,
                                                             capsys):
    """E12. "window start unknown" is the one thing that is actually wrong
    about this row: the clause gives a start, it is simply ahead of us.

    Note the arithmetic, which is the easy thing to get wrong here. The
    window starts at `resets - window`, so a clause 20h out on a 5h window
    starts in 15h, not 20.
    """
    folder = status_folder(RESETS, {"session": session_bucket(
        50, resets=RESETS + 20 * 3600)})
    assert cli.cmd_status(folder) == 0
    out = capsys.readouterr().out
    assert "window starts in 15h00m" in out
    assert "window start unknown" not in out


def test_status_still_distinguishes_a_merely_missing_reset_clause(status_folder,
                                                                  capsys):
    """The two share a None `elapsed` and nothing else. A freshly rolled
    window with no clause yet is judged against the m0 floor, which is a real
    (if crude) line; a void reading is judged against nothing at all."""
    folder = status_folder(HALFWAY, {"session": {"pct": 3, "resets_epoch": None,
                                                 "window_seconds": SESSION_WINDOW,
                                                 "label": None}})
    assert cli.cmd_status(folder) == 0
    out = capsys.readouterr().out
    assert "window start unknown" in out
    assert "reading is void" not in out


# --- E6: an expired bucket forces one refresh, and only one ------------------

class _Clock:
    """Advances only when the code under test sleeps, so a hold that would
    really cost minutes costs nothing here."""

    def __init__(self, start):
        self.t = float(start)
        self.naps = []

    def time(self):
        return self.t

    def sleep(self, seconds):
        self.naps.append(seconds)
        if len(self.naps) > 500:
            raise AssertionError("run() never released; it is still holding")
        self.t += seconds


@pytest.fixture
def held(cwd, tmp_path, monkeypatch):
    """A full `hook.run` with policy, snapshot, refresh and clock faked.

    By default the refresh changes nothing, so the hold runs to a cap rather
    than being cured. `refresh_ok=True` is the arrangement that makes the
    `first` gate visible on its own: a SUCCESSFUL refresh resets next_try to
    0, so nothing but that gate stands between the hold and a ~2s subprocess
    on every chunk. `refresh_ok=False` is the other half, where the backoff
    is in play as well.

    `cured` is the snapshot a SUCCESSFUL refresh swaps in, for the case the
    whole refresh force exists to reach. Without it no arrangement here can
    tell a hook that refreshes and reads the result from one that refreshes
    and ignores it.

    `age` may be a callable of the clock, for the one claim a constant cannot
    express: a snapshot fresh when the hold begins and stale later, which is
    what separates the force from the ordinary staleness gate.

    Returns (reason, refresh times, clock).
    """
    policy_path = tmp_path / "policy.json"
    monkeypatch.setattr(hook, "POLICY_PATH", str(policy_path))
    monkeypatch.setattr(hook, "log", lambda msg: None)

    def go(buckets, start, entry=None, age=5, refresh_ok=True, cured=None):
        policy_path.write_text(json.dumps(policy_for(cwd, entry)),
                               encoding="utf-8")
        clock = _Clock(start)
        refreshes = []
        live = {"buckets": buckets}
        aged = age if callable(age) else (lambda _t: age)

        def refresh():
            refreshes.append(clock.t)
            if refresh_ok and cured is not None:
                live["buckets"] = cured
            return refresh_ok

        monkeypatch.setattr(
            hook, "load_state",
            lambda path, key: ({"ts_epoch": clock.t - aged(clock.t),
                                "buckets": live["buckets"]}, None))
        monkeypatch.setattr(hook, "snapshot_age",
                            lambda state, now: aged(clock.t))
        monkeypatch.setattr(hook, "refresh_snapshot", refresh)
        monkeypatch.setattr(hook.time, "time", clock.time)
        monkeypatch.setattr(hook.time, "sleep", clock.sleep)
        _brake_start, reason = hook.run(cwd)
        return reason, refreshes, clock

    return go


def test_an_expired_bucket_forces_a_refresh_on_the_first_pass(held, cwd):
    """E6. An expired reading voids CONTENT without aging the FILE, so the
    freshness gate cannot see it: `limit` tightens to NEAR_STALE only once
    the region is already band or over, and a snapshot younger than that is
    never re-read. The stall is at the exact moment a fresh window became
    available, and it costs a full blind_delay where a ~2s refresh would have
    cured it -- what that refresh reaches is the post-roll sample's MISSING
    reset clause, which bucket_pace already judges against the m0 floor.
    """
    entry = {"paced": True, "model": "opus", "blind_delay": 60}
    reason, refreshes, _clock = held({"session": session_bucket(95)},
                                     EXPIRED_NOW, entry=entry, age=5)
    assert reason == "blind-release"
    assert len(refreshes) == 1
    assert refreshes[0] == EXPIRED_NOW          # the first pass, before any nap


def test_the_forced_refresh_does_not_repeat_on_later_passes(held, cwd):
    """The `first` gate, and it is load-bearing. The refresh above succeeded,
    which sets next_try back to 0, so an ungated force would re-run a ~2s
    subprocess on every chunk for as long as the window stayed expired. One
    refresh per invocation is the guarantee the rest of `run` already makes:
    every completed step is followed by a fresh invocation.
    """
    entry = {"paced": True, "model": "opus", "blind_delay": 60}
    _reason, refreshes, clock = held({"session": session_bucket(95)},
                                     EXPIRED_NOW, entry=entry, age=5)
    assert len(clock.naps) > 1           # several passes really did happen
    assert len(refreshes) == 1           # and only the first one looked


@pytest.mark.parametrize("entry,label", [
    ({"blind_delay": None, "max_delay": 30}, "max_delay alone"),
    ({"blind_delay": 240, "max_delay": 30}, "max_delay tighter than blind"),
])
def test_an_expired_hold_is_also_bounded_by_max_delay(held, cwd, entry, label):
    """E3. blind_delay is not the only way out of a void hold, and it is the
    only one the other tests here ever observe.

    With blind_delay off, max_delay is what ends it -- a folder that has
    turned the blind ceiling off has not thereby asked to stall. With both
    set, the tighter wins, as every cap in this codebase does. The label is
    the WHILE-BLIND variant either way, and deliberately: the knob that fired
    was max_delay, but the reason the call went through unpaced is that
    nothing could see, and that is what a reader of hook.log has to act on.
    """
    rule = {"paced": True, "model": "opus"}
    rule.update(entry)
    reason, _refreshes, clock = held({"session": session_bucket(95)},
                                     EXPIRED_NOW, entry=rule)
    assert reason == "max_delay-release-WHILE-BLIND", label
    assert clock.t - EXPIRED_NOW == 30, label


def test_a_forced_refresh_that_fails_is_still_attempted_only_once(held, cwd):
    """E6. The force runs at most once because `first` is cleared right after
    it, whatever the refresh returned -- the other tests here only ever watch
    it succeed, and a successful refresh zeroes next_try, so they cannot tell
    the `first` gate from the backoff.

    Here it fails, and the two suppressors do NOT agree, which is what makes
    this worth keeping. REFRESH_BACKOFF[1] is 15 and the chunk is 15, so the
    backoff would permit a second look at t+15 and every pass after it; the
    `first` gate alone is what stops them. The hold runs out its blind_delay
    on one attempt.
    """
    reason, refreshes, clock = held(
        {"session": session_bucket(95)}, EXPIRED_NOW,
        entry={"paced": True, "model": "opus", "blind_delay": 60},
        age=5, refresh_ok=False)
    assert reason == "blind-release"
    assert refreshes == [EXPIRED_NOW]        # once, on the first pass
    assert clock.naps == [15.0, 15.0, 15.0, 15.0]   # four passes, one look


def test_later_passes_fall_back_to_the_staleness_gate_and_its_backoff(held,
                                                                      cwd):
    """E6, the other half: the force looks once, and the ordinary machinery
    takes over.

    The snapshot is FRESH on the first pass -- five seconds old, well inside
    NEAR_STALE -- so nothing but the force can refresh there. It then ages
    out, as it does when the daemon dies under a hold, and every later look
    comes from the `age > MAX_STALE` gate on the REFRESH_BACKOFF schedule
    rather than once per chunk.

    A constant age cannot express that. With the snapshot stale from the
    start, `age > limit` is already true on the first pass and the force
    never contributes anything -- the assertions below then hold just as well
    for a free bucket, or an empty snapshot, with the whole feature removed.
    """
    _reason, refreshes, clock = held(
        {"session": session_bucket(95)}, EXPIRED_NOW,
        entry={"paced": True, "model": "opus", "blind_delay": 240},
        age=lambda t: 5 if t == EXPIRED_NOW else 9999, refresh_ok=False)
    offsets = [t - EXPIRED_NOW for t in refreshes]
    # The first look happens on the first pass, where only the force could
    # have run it.
    assert offsets[0] == 0.0
    gaps = [b - a for a, b in zip(offsets, offsets[1:])]
    assert gaps == list(hook.REFRESH_BACKOFF[1:5])
    assert len(clock.naps) > len(refreshes)   # far more passes than looks


def test_a_forced_refresh_that_finds_a_new_window_ends_the_hold(held, cwd):
    """E6/E8, and the whole justification for the force: what the refresh
    reaches is the post-roll sample, whose MISSING reset clause bucket_pace
    judges against the m0 floor -- so a few percent into a fresh window is
    under it and the call goes straight through.

    Nothing else here can see that. Every other arrangement returns the same
    void reading however the refresh went, so a hook that fired the force and
    then ignored what came back would look identical. This is also the claim
    `status` now prints verbatim: "it only actually holds here if that
    refresh fails".
    """
    reason, refreshes, clock = held(
        {"session": session_bucket(95)}, EXPIRED_NOW,
        entry={"paced": True, "model": "opus", "blind_delay": 60},
        cured={"session": {"pct": 3, "resets_epoch": None,
                           "window_seconds": None, "label": None}})
    assert reason == "line-caught-up"
    assert refreshes == [EXPIRED_NOW]
    assert clock.naps == []                   # never held at all


def test_a_fresh_snapshot_over_the_line_still_refreshes_nothing(held, cwd):
    """The control the test above needs to mean anything: the same region,
    the same age, no expiry -- and the freshness gate alone declines to look,
    because NEAR_STALE is 15s and the snapshot is 5s old. So the refresh in
    the expired case is the force doing it, not the gate."""
    assert NEAR_STALE == 15
    entry = {"paced": True, "model": "opus", "max_delay": 30}
    reason, refreshes, _clock = held({"session": session_bucket(90)},
                                     HALFWAY, entry=entry, age=5)
    assert reason == "max_delay-release"
    assert refreshes == []
