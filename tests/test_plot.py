"""Which buckets `plot` draws, and in what order.

`plot` used to hardcode its panel list to session and week:all models, so the
per-model weekly bucket was parsed, collected, and then silently dropped on the
way to the figure. On the log this was written against that was the worst
possible omission: week:all models never once crossed the line (0/8854 live
samples) while week:Fable, the bucket actually governing the same work, sat
above it 46% of the time and overshot by 35 points. The plot said everything
was fine because the only bucket in trouble was the one it did not draw.

Nothing here imports matplotlib. Panel selection is the part that was wrong, it
is pure, and the optional extra must not decide whether the suite can check it.
"""

from datetime import datetime, timezone

from niceclaude import cli
from niceclaude.plot import PALETTE, _slot, collect, panels_for, per_model_keys

NOW = datetime(2026, 9, 16, 12, 0, 0, tzinfo=timezone.utc)

# Verbatim shape of a real `/usage` reply, separator written as an escape so
# this file stays pure ASCII -- see tests/test_source_encoding.py.
RAW = (
    "Current session: 10% used \u00b7 resets Sep 16, 9:10pm (UTC)\n"
    "Current week (all models): 29% used \u00b7 resets Sep 19, 8pm (UTC)\n"
    "Current week (Fable): 54% used \u00b7 resets Sep 19, 8pm (UTC)\n"
)


def record(raw, ts_epoch=1789000000, exit_code=0):
    return {"ts_epoch": ts_epoch, "raw": raw, "exit_code": exit_code}


# --- per_model_keys ----------------------------------------------------------

def test_per_model_key_is_found_by_shape_not_by_name():
    """The label is server-supplied, so it cannot be matched against a list."""
    assert per_model_keys({"week:Fable": []}) == ["week:Fable"]
    assert per_model_keys({"week:Sonnet only": []}) == ["week:Sonnet only"]
    assert per_model_keys({"week:Opus 4.1": []}) == ["week:Opus 4.1"]


def test_shared_weekly_bucket_is_not_per_model():
    """It is the bucket the per-model ones live inside, not one of them."""
    assert per_model_keys({"week:all models": []}) == []
    assert per_model_keys({"week:All Models": []}) == []
    assert per_model_keys({"week:  all models  ": []}) == []


def test_session_is_never_per_model():
    assert per_model_keys({"session": [], "session:something": []}) == []


def test_order_does_not_depend_on_dict_insertion():
    """Two runs over the same log must not produce two different figures."""
    forward = per_model_keys({"week:Fable": [], "week:Sonnet only": []})
    reverse = per_model_keys({"week:Sonnet only": [], "week:Fable": []})
    assert forward == reverse == ["week:Fable", "week:Sonnet only"]


# --- panels_for --------------------------------------------------------------

def test_panel_order_puts_the_per_model_bucket_last():
    series = {"week:Fable": [], "session": [], "week:all models": []}
    assert panels_for(series) == ["session", "week:all models", "week:Fable"]


def test_the_regression_itself():
    """The per-model bucket reaches the figure at all."""
    series = collect([record(RAW)], cli.parse_usage)
    assert "week:Fable" in series
    assert "week:Fable" in panels_for(series)
    assert series["week:Fable"][0]["pct"] == 54.0


def test_a_log_with_only_a_per_model_bucket_still_plots():
    """`panels` empty is the one case render() refuses outright, so a log that
    somehow carries only this bucket must not land there."""
    assert panels_for({"week:Fable": []}) == ["week:Fable"]


def test_missing_buckets_are_skipped_not_faked():
    assert panels_for({"session": []}) == ["session"]
    assert panels_for({}) == []


# --- colour assignment -------------------------------------------------------

def test_each_bucket_gets_its_own_hue():
    hues = [_slot(i) for i in range(len(PALETTE))]
    assert len(set(hues)) == len(PALETTE)


def test_the_fixed_order_does_not_cycle():
    """Cycling would paint two different buckets the same colour, which is the
    single thing a fixed categorical order exists to prevent."""
    beyond = _slot(len(PALETTE))
    assert beyond not in PALETTE
