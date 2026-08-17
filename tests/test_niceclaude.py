"""Tests for the parser, the model/path matching, and the pace-line decision.

Nothing here touches the network, the real clock, or the user's data directory:
`parse_usage` and `decide` both take an explicit `now`, which is the whole
reason they are separable from the polling loop.
"""

import os
from datetime import datetime, timezone

import pytest

from niceclaude import cli
from niceclaude import hook
from niceclaude._shared import model_matches, norm_path

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
    # pess = pct + 1 must be strictly above the line to brake.
    pct = ALLOWED_AT_HALFWAY - 1
    d = hook.decide(policy_for(cwd), state_for({"session": session_bucket(pct)}),
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
