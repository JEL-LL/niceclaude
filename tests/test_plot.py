"""Which buckets `plot` draws, in what order, and over what span.

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

import argparse
from datetime import datetime, timezone

import pytest

from niceclaude import cli
from niceclaude.plot import (
    PALETTE, _slot, clip, collect, panels_for, per_model_keys,
)

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


# --- clip --------------------------------------------------------------------

DAY = 86400


def live(ts):
    return record(RAW, ts_epoch=ts)


def dead(ts):
    return record("", ts_epoch=ts, exit_code=1)


def test_no_days_keeps_the_whole_log():
    records = [live(0), live(DAY), live(9 * DAY)]
    assert clip(records, None) == records


def test_days_keeps_the_tail():
    records = [live(0), live(5 * DAY), live(9 * DAY), live(10 * DAY)]
    kept = clip(records, 7)
    assert [r["ts_epoch"] for r in kept] == [5 * DAY, 9 * DAY, 10 * DAY]


def test_the_cutoff_is_inclusive():
    """A sample exactly `days` old is inside the window, not over the edge."""
    records = [live(0), live(7 * DAY)]
    assert len(clip(records, 7)) == 2
    assert len(clip(records, 6.99)) == 1


def test_fractional_days_are_hours():
    records = [live(0), live(DAY // 2), live(DAY)]
    assert len(clip(records, 0.5)) == 2


def test_the_anchor_is_the_newest_sample_not_the_wall_clock():
    """A log that stopped a month ago still yields that log's own last week,
    rather than the empty figure a clock-anchored window would draw."""
    stale = [live(t * DAY) for t in range(31)]
    kept = clip(stale, 7)
    # Day 23 is exactly seven days before the newest sample, and the cutoff is
    # inclusive, so a seven-day window spans eight daily samples.
    assert [r["ts_epoch"] for r in kept] == [t * DAY for t in range(23, 31)]


def test_a_positive_days_never_empties_a_log_that_had_samples():
    """The anchor is itself a sample, so it is always inside its own window.
    That leaves exactly one route to "nothing to plot" -- an empty log -- and
    a --days that silently drew nothing would be the worst of both."""
    records = [live(0), live(100 * DAY)]
    for days in (0.001, 1, 7, 30, 3650):
        assert clip(records, days), f"--days {days} emptied the log"


def test_a_failed_sample_is_not_the_anchor():
    """collect() drops failed samples, so anchoring the window on a trailing
    run of them would clip away everything that could still be drawn."""
    records = [live(0), live(DAY), dead(40 * DAY)]
    kept = clip(records, 7)
    assert [r["ts_epoch"] for r in kept] == [0, DAY, 40 * DAY]


def test_a_log_with_nothing_successful_is_left_alone():
    """Nothing to anchor on; collect() then reports the empty log itself."""
    records = [dead(0), dead(DAY)]
    assert clip(records, 1) == records


def test_clipping_happens_before_parsing():
    """The point of clipping records rather than the collected series: the
    regex never runs over the part of the log being thrown away."""
    records = [live(0), live(30 * DAY)]
    seen = []

    def spy(raw, when):
        seen.append(raw)
        return cli.parse_usage(raw, when)

    collect(clip(records, 7), spy)
    assert len(seen) == 1


# --- the --days flag ---------------------------------------------------------

def test_the_flag_reaches_the_parser():
    ap, _sub = cli.build_parser()
    assert ap.parse_args(["plot", "--days", "7"]).days == 7.0
    assert ap.parse_args(["plot"]).days is None


@pytest.mark.parametrize("text", ["7", "30", "0.5", "1e2"])
def test_days_accepts_a_positive_number(text):
    assert cli.positive_days(text) == float(text)


@pytest.mark.parametrize("text", ["0", "-1", "-0.5"])
def test_days_refuses_a_window_that_holds_nothing(text):
    with pytest.raises(argparse.ArgumentTypeError):
        cli.positive_days(text)


@pytest.mark.parametrize("text", ["abc", "", "7 days", "seven"])
def test_days_refuses_a_non_number(text):
    with pytest.raises(argparse.ArgumentTypeError):
        cli.positive_days(text)
