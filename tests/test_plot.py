"""Which buckets `plot` draws, in what order, and over what span.

`plot` used to hardcode its panel list to session and week:all models, so the
per-model weekly bucket was parsed, collected, and then silently dropped on the
way to the figure. On the log this was written against that was the worst
possible omission: week:all models never once crossed the line (0/8854 live
samples) while week:Fable, the bucket actually governing the same work, sat
above it 46% of the time and overshot by 35 points. The plot said everything
was fine because the only bucket in trouble was the one it did not draw.

Almost nothing here imports matplotlib. Panel selection is the part that was
wrong, it is pure, and the optional extra must not decide whether the suite can
check it; the band tests below use a recording stand-in for an Axes for the
same reason, since what has to be proven is which strokes are asked for rather
than how they rasterize. Exactly one test needs a real figure, and skips
without one.
"""

import argparse
from datetime import datetime, timezone

import pytest

from niceclaude import cli
from niceclaude._shared import (
    DEFAULT_BAND, DEFAULT_M0, DEFAULT_M1, bucket_pace, norm_path,
)
from niceclaude.plot import (
    PALETTE, _draw_overlay_band, _draw_window, _pace, _slot, clip, collect,
    panels_for, per_model_keys, render,
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


# --- the throttle band -------------------------------------------------------

WEEK = 7 * DAY


def sample(ts, pct, resets=WEEK, window=WEEK):
    return {"ts_epoch": ts, "pct": pct, "resets_epoch": resets,
            "window_seconds": window}


class FakeAx:
    """Enough of an Axes to record what the drawing helpers ask it for."""

    def __init__(self):
        self.plots, self.fills = [], []

    def plot(self, xs, ys, **kw):
        self.plots.append((list(xs), list(ys), kw))

    def fill_between(self, xs, lo, hi, **kw):
        self.fills.append((list(xs), list(lo), list(hi), kw))


# x, y, allowed, low -- one window's worth of what render() collects
RUN = [(0, 10.0, 12.0, 8.0), (1, 30.0, 20.0, 16.0)]


def test_the_drawn_lines_are_the_hook_s_own_arithmetic():
    """The plot must not carry a second copy of the pace geometry. It used to
    carry its own brake line, and a chart that disagrees with the controller
    it is drawn to explain is worse than no chart."""
    p = sample(WEEK // 3, 12.0)
    truth = bucket_pace(p, p["ts_epoch"], 5, 8, 3)
    assert _pace(p, 5, 8, 3) == (truth["allowed"], truth["low"])


def test_without_a_band_the_two_lines_coincide():
    allowed, low = _pace(sample(WEEK // 2, 20.0), 5, 8, 0)
    assert low == allowed


def test_the_throttle_line_sits_the_band_below_the_brake_line():
    allowed, low = _pace(sample(WEEK // 2, 20.0), 5, 8, 4)
    assert low == pytest.approx(allowed - 4)


def test_the_band_is_floored_at_the_grubstake():
    """m0 is what makes a fresh window startable at all, so the band opens as
    the window advances rather than throttling its first step."""
    allowed, low = _pace(sample(60, 0.0), 5, 8, 4)
    assert low == 5
    assert allowed < 5 + 4


def test_a_sample_with_no_reset_clause_still_has_no_line():
    """bucket_pace collapses both lines onto m0 there; the panel shades the
    span instead, and a flat line at the grubstake would claim a pace line
    that was never in force."""
    assert _pace(sample(1, 40.0, resets=None, window=None), 5, 8, 3) is None


@pytest.mark.parametrize("band", [0, None])
def test_band_zero_draws_exactly_what_it_drew_before(band):
    """The opt-out has to be invisible: one dashed line and the over-the-line
    shading, which is the whole of what a panel held before the band existed.
    None is the other way a policy can say no."""
    ax = FakeAx()
    _draw_window(ax, RUN, band)
    assert len(ax.plots) == 1
    assert ax.plots[0][1] == [12.0, 20.0]        # the brake line, nothing else
    assert len(ax.fills) == 1                    # only the over-the-line region


def test_a_band_adds_a_second_line_and_the_area_between_them():
    """Shaded, not merely stroked: the question a band raises is how much of
    the run was spent between the lines, which two strokes do not answer."""
    ax = FakeAx()
    _draw_window(ax, RUN, 4)
    assert [p[1] for p in ax.plots] == [[8.0, 16.0], [12.0, 20.0]]
    _xs, lows, highs, _kw = ax.fills[0]
    assert (lows, highs) == ([8.0, 16.0], [12.0, 20.0])


def test_the_overlay_says_nothing_about_a_band_that_is_off():
    """Including in the legend -- a user who has not opted in must not be told
    about a feature that is doing nothing."""
    ax = FakeAx()
    _draw_overlay_band(ax, 5, 8, 0)
    assert not ax.plots and not ax.fills


def test_the_overlay_band_spans_the_same_two_lines():
    ax = FakeAx()
    _draw_overlay_band(ax, 5, 8, 4)
    _xs, lows, highs, kw = ax.fills[0]
    assert "band" in kw["label"]
    assert (lows[0], highs[0]) == (5, 5)         # both start on the grubstake
    assert highs[-1] == pytest.approx(92)        # 100 - m1, the line's end
    assert lows[-1] == pytest.approx(88)
    assert min(lows) == 5                        # never under the grubstake


def test_a_figure_with_a_band_renders(tmp_path):
    """The one test that needs the optional extra. FakeAx cannot catch a
    keyword matplotlib would reject, and `plot` failing at the last call in
    the run is how that would otherwise be found."""
    pytest.importorskip("matplotlib")
    records = [record(RAW, ts_epoch=1789000000 + i * 3600) for i in range(6)]
    series = collect(records, cli.parse_usage)
    out = tmp_path / "band.png"
    assert render(series, str(out), 5, 8, 3) == 0
    assert out.stat().st_size > 0


# --- the line the figure defaults to -----------------------------------------
#
# `plot` used to draw every log against the built-in 5/8/0 no matter how the
# folder was actually paced, so a run held to a configured line was graded
# against a line that was never in force -- the plot calling samples over
# budget that the hook had let through, or the reverse. The default now comes
# from the same place `status` reads, and the flags override it.

@pytest.fixture
def policy(tmp_path, monkeypatch):
    """A redirected policy file, and a folder that `plot` will think it is in.

    cwd is faked rather than actually changed: geometry_for asks os.getcwd(),
    and a chdir would leak into the rest of the session.
    """
    monkeypatch.setattr(cli, "POLICY_PATH", str(tmp_path / "policy.json"))
    proj = tmp_path / "proj"
    proj.mkdir()
    monkeypatch.setattr(cli.os, "getcwd", lambda: str(proj))

    def write(paths=None, defaults=None):
        cli.save_policy({"global": {"enabled": True},
                         "defaults": defaults if defaults is not None else {},
                         "paths": paths or {}})

    return proj, write


def rule(proj, **fields):
    return {norm_path(str(proj)): dict(paced=True, **fields)}


def test_the_folders_own_rule_supplies_the_line(policy):
    proj, write = policy
    write(rule(proj, m0=12, m1=3, band=4))
    matched, m0, m1, band = cli.geometry_for(str(proj))
    assert matched == norm_path(str(proj))
    assert (m0, m1, band) == (12, 3, 4)


def test_a_rule_inherits_what_it_does_not_set(policy):
    """A rule naming only m0 must not drag the other two back to the built-in
    constants: the policy defaults sit between them, and that middle layer is
    what the hook itself reads."""
    proj, write = policy
    write(paths=rule(proj, m0=12), defaults={"m1": 3, "band": 4})
    _matched, m0, m1, band = cli.geometry_for(str(proj))
    assert (m0, m1, band) == (12, 3, 4)


def test_a_subfolder_inherits_the_rule_above_it(policy):
    """The same longest-prefix resolution `status` uses -- plot must not have
    its own idea of which rule covers a folder."""
    proj, write = policy
    write(rule(proj, m0=12, m1=3))
    sub = proj / "src" / "deep"
    sub.mkdir(parents=True)
    matched, m0, m1, _band = cli.geometry_for(str(sub))
    assert matched == norm_path(str(proj))
    assert (m0, m1) == (12, 3)


def test_an_uncovered_folder_still_gets_the_policy_defaults(policy):
    """Not paced, so nothing is in force -- but the defaults are the numbers a
    rule written here would inherit, and the log in front of you was recorded
    on a machine configured with them."""
    proj, write = policy
    write(paths={}, defaults={"m0": 12, "m1": 3, "band": 4})
    matched, m0, m1, band = cli.geometry_for(str(proj))
    assert matched is None
    assert (m0, m1, band) == (12, 3, 4)


def test_an_empty_policy_falls_all_the_way_to_the_built_ins(policy):
    proj, write = policy
    write()
    _matched, m0, m1, band = cli.geometry_for(str(proj))
    assert (m0, m1, band) == (DEFAULT_M0, DEFAULT_M1, DEFAULT_BAND)


def test_a_null_band_reads_as_no_band(policy):
    """`on --no-band-delay` writes explicit nulls, so a hand-edited null band
    is reachable. It means what 0 means to every reader of this line, and
    render clamps it that way -- formatting a None as a number would crash."""
    proj, write = policy
    write(rule(proj, band=None))
    assert cli.geometry_for(str(proj))[3] == 0


def test_status_and_plot_read_the_same_three(policy):
    """The guarantee the shared reader exists for: a chart that grades a run
    against a different line than `status` prices is worse than no chart."""
    proj, write = policy
    write(paths=rule(proj, m0=12), defaults={"m1": 3, "band": 4})
    pol = cli.load_policy()
    _matched, entry = cli.resolve(pol, norm_path(str(proj)))
    assert cli.line_geometry(pol, entry) == cli.geometry_for(str(proj))[1:]


# --- the flags still win -----------------------------------------------------

def test_the_geometry_flags_default_to_unset():
    """None, not the built-in constants: it is the only way to tell "the user
    asked for m0=5" from "the user asked for nothing" once the default is a
    folder's configuration rather than a literal."""
    ap, _sub = cli.build_parser()
    a = ap.parse_args(["plot"])
    assert (a.m0, a.m1, a.band) == (None, None, None)
    a = ap.parse_args(["plot", "--m0", "12", "--band", "4"])
    assert (a.m0, a.m1, a.band) == (12.0, None, 4.0)


@pytest.fixture
def drawn(monkeypatch):
    """Run cmd_plot against a stubbed renderer and report the line it asked
    for. The log is stubbed empty too: what is under test is which numbers
    reach render, not what they draw."""
    from niceclaude import plot as plotmod
    calls = []
    monkeypatch.setattr(cli, "load_log", lambda: [])
    monkeypatch.setattr(plotmod, "render",
                        lambda series, out, m0, m1, band:
                        calls.append((m0, m1, band)) or 0)

    def run(**kw):
        args = dict(out="x.png", days=None, m0=None, m1=None, band=None)
        args.update(kw)
        assert cli.cmd_plot(**args) == 0
        return calls[-1]

    return run


def test_with_no_flags_the_figure_takes_the_folders_line(policy, drawn):
    proj, write = policy
    write(rule(proj, m0=12, m1=3, band=4))
    assert drawn() == (12, 3, 4)


def test_a_flag_overrides_only_itself(policy, drawn):
    """--m0 redraws against an m0 you are considering; it must not also reset
    the band you are actually paced with, or the run is being compared against
    two changes at once."""
    proj, write = policy
    write(rule(proj, m0=12, m1=3, band=4))
    assert drawn(m0=20) == (20, 3, 4)


def test_a_zero_flag_is_an_override_not_an_absence(policy, drawn):
    """What a None default buys: `--band 0` asks for the brake line alone, and
    is falsy, so `or` would have silently restored the configured band."""
    proj, write = policy
    write(rule(proj, band=4))
    assert drawn(band=0)[2] == 0


def test_the_line_it_took_is_printed_before_it_draws(policy, drawn, capsys):
    """The default now lives in a file the reader cannot see from the command
    line, so the run has to say whose line the figure was drawn against."""
    proj, write = policy
    write(rule(proj, m0=12, m1=3, band=4))
    drawn(band=2)
    out = capsys.readouterr().out
    assert "m0=12, m1=3, band=2" in out
    assert norm_path(str(proj)) in out          # which rule it came from
    assert "--band from the command line" in out


def test_a_fully_overridden_line_credits_no_rule(policy, drawn, capsys):
    """With all three given the rule supplied nothing, and saying otherwise
    would send a reader to a policy file to explain numbers they typed."""
    proj, write = policy
    write(rule(proj, m0=12, m1=3, band=4))
    assert drawn(m0=20, m1=5, band=1) == (20, 5, 1)
    out = capsys.readouterr().out
    assert "all from the command line" in out
    assert "rule" not in out


def test_an_uncovered_folder_says_so(policy, drawn, capsys):
    proj, write = policy
    write(paths={})
    drawn()
    assert "no rule covers this folder" in capsys.readouterr().out
