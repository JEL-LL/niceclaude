"""Per-folder selection of which usage windows a folder is paced against.

Motivating case: several foreground projects tended round-robin. You want the
5-hour line to smooth each one out, but the weekly line exists to protect budget
for days you are *not* here, so it should not throttle work you are actively
doing.
"""

import time

import pytest

from niceclaude._shared import DEFAULT_ENFORCE, normalize_enforce
from niceclaude import hook

NOW = 1_800_000_000
SESSION_WINDOW = 5 * 3600
WEEK_WINDOW = 7 * 86400


def bucket(pct, window, elapsed_fraction=0.5):
    """A bucket `elapsed_fraction` through its window."""
    start = NOW - int(window * elapsed_fraction)
    return {"pct": pct, "resets_epoch": start + window,
            "window_seconds": window, "label": None}


def policy(enforce=None, model="opus"):
    entry = {"paced": True, "model": model}
    if enforce is not None:
        entry["enforce"] = enforce
    return {"paths": {"/w": entry}}


def state(session_pct, week_pct, fable_pct=None):
    buckets = {
        "session": bucket(session_pct, SESSION_WINDOW),
        "week:all models": bucket(week_pct, WEEK_WINDOW),
    }
    if fable_pct is not None:
        buckets["week:Fable"] = bucket(fable_pct, WEEK_WINDOW)
    return {"ts_epoch": NOW, "buckets": buckets}


def decide(pol, st, event=None):
    return hook.decide(pol, st, hook.norm_path("/w"), NOW, event=event)


# --- normalize_enforce -------------------------------------------------------

@pytest.mark.parametrize("value,expected", [
    (None,                     set(DEFAULT_ENFORCE)),
    ("session",                {"session"}),
    ("session,week",           {"session", "week"}),
    (" SESSION , Week ",       {"session", "week"}),
    (["session", "model"],     {"session", "model"}),
    ("session,bogus",          {"session"}),
])
def test_normalize_accepts_known_forms(value, expected):
    assert normalize_enforce(value) == expected


@pytest.mark.parametrize("value", ["", [], "bogus", ["nonsense"], 42, {}])
def test_malformed_config_falls_back_to_enforcing_everything(value):
    """Fail safe. A config that names nothing recognizable must not silently
    un-pace a folder that still reports itself as paced."""
    assert normalize_enforce(value) == set(DEFAULT_ENFORCE)


# --- what decide() actually enforces ----------------------------------------

def test_default_brakes_on_the_weekly_line():
    d = decide(policy(), state(session_pct=2, week_pct=95))
    assert d["braked"] is True
    assert "week:all models" in d["reason"]


def test_session_only_ignores_a_blown_weekly_budget():
    """The round-robin case: weekly is exhausted, but this project is being
    actively worked on and only answers to the 5-hour line."""
    d = decide(policy(enforce="session"), state(session_pct=2, week_pct=95))
    assert d["braked"] is False


def test_session_only_still_brakes_on_its_own_line():
    d = decide(policy(enforce="session"), state(session_pct=80, week_pct=1))
    assert d["braked"] is True
    assert "session" in d["reason"]


def test_week_only_ignores_a_blown_session():
    d = decide(policy(enforce="week"), state(session_pct=90, week_pct=1))
    assert d["braked"] is False


def test_model_bucket_can_be_enforced_alone():
    d = decide(policy(enforce="model", model="fable"),
               state(session_pct=90, week_pct=90, fable_pct=95))
    assert d["braked"] is True
    assert "Fable" in d["reason"]


def test_model_bucket_excluded_when_not_enforced():
    d = decide(policy(enforce="session", model="fable"),
               state(session_pct=1, week_pct=99, fable_pct=99))
    assert d["braked"] is False


def test_enforcing_nothing_available_is_blind_not_permissive():
    """If the chosen window is absent from the snapshot we cannot judge it, so
    the fail-safe must brake rather than wave the agent through."""
    st = {"ts_epoch": NOW, "buckets": {
        "week:all models": bucket(1, WEEK_WINDOW)}}      # no session bucket
    d = decide(policy(enforce="session"), st)
    assert d["braked"] is True
    assert d["blind"] is True


def test_fanout_reserve_still_applies_within_the_chosen_windows():
    """The two features compose: a stricter bar for spawns, applied only to the
    windows this folder answers to."""
    pol = {"paths": {"/w": {"paced": True, "model": "opus",
                            "enforce": "session", "fanout_reserve": 60}}}
    st = state(session_pct=40, week_pct=99)
    assert decide(pol, st, event="PreToolUse")["braked"] is False
    assert decide(pol, st, event="SubagentStart")["braked"] is True
