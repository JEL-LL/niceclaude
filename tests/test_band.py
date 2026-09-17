"""The band: a second pace line below the brake line.

The old controller had one line. A hold ended the moment it reached `pess`, so
the agent resumed with under one quantum of headroom and the next 1% tick put it
over again -- on the weekly line that is a 1.68h hold for every 1% of budget,
each one long enough to kill the prompt cache. Every 1% therefore cost a full
cold context re-read, and `max_delay` was the only escape: it buys cache warmth
by spending the ceiling, proceeding while still over the line.

The band buys the same warmth without spending anything. The brake line keeps
its exact meaning -- never above it -- and a throttle line is drawn `band`
points beneath it. Both braking regions aim at the THROTTLE line, so one hold
now buys a whole band of running instead of one quantum, and everything the
feature does happens safely below the guarantee.

Two knobs, deliberately orthogonal:

    band        geometry. 0 is the previous behaviour, exactly.
    band_delay  what one hold costs inside the band. None means the band is
                pure release hysteresis: run through it at full speed.

Both now ship set (7 points, 180s), so a test that wants either one off says
so: `paced(band=BAND, band_delay=None)` writes the explicit null the CLI's
--no-band-delay writes, and inheriting is what the bare entry gets.

The clock is virtual throughout, as in test_max_delay.py: a test that actually
slept would take as long as the behaviour it is asserting.
"""

import json

import pytest

from niceclaude import hook
from niceclaude._shared import (
    DEFAULT_BAND, DEFAULT_BAND_DELAY, bucket_pace, norm_path,
)

SESSION_WINDOW = 5 * 3600
RESETS = 1_760_000_000                      # fixed epoch; never the real clock
START = RESETS - SESSION_WINDOW
HALFWAY = START + SESSION_WINDOW / 2        # f_t = 0.5

WEEK_WINDOW = 7 * 86400
WEEK_RESETS = RESETS + 4 * 86400            # four days out, as a weekly bucket is
WEEK_START = WEEK_RESETS - WEEK_WINDOW

M0 = 5
M1 = 8
SPAN = 100 - M0 - M1                        # 87

# At the halfway point the brake line stands at exactly 5 + 0.5 * 87 = 48.5.
# With the band below used through most of this file, the throttle line is 43.5.
ALLOWED_AT_HALFWAY = 48.5
BAND = 5

FREE_PCT = 30       # pess 31, under the throttle line
BAND_PCT = 45       # pess 46, between the lines
HOT_PCT = 90        # pess 91, far above the brake line; its solved release is
                    # the window's own reset, hours past any cap tested here


def session_bucket(pct):
    return {"pct": pct, "resets_epoch": RESETS,
            "window_seconds": SESSION_WINDOW, "label": None}


def week_bucket(pct):
    return {"pct": pct, "resets_epoch": WEEK_RESETS,
            "window_seconds": WEEK_WINDOW, "label": "all models"}


def floating_bucket(pct):
    """A bucket with no reset clause -- what a freshly rolled window looks like
    until /usage starts reporting one again. f_t is unknown, so neither line can
    be drawn."""
    return {"pct": pct, "resets_epoch": None, "window_seconds": None,
            "label": None}


def at(fraction, start=START, window=SESSION_WINDOW):
    """The epoch at which `fraction` of the window has elapsed."""
    return start + fraction * window


def policy_for(cwd, entry=None, defaults=None):
    return {
        "global": {"enabled": True},
        "defaults": defaults if defaults is not None else {"m0": M0, "m1": M1,
                                                           "chunk": 15},
        "paths": {cwd: entry if entry is not None else {"paced": True,
                                                        "model": "opus"}},
    }


def state_for(buckets, now=HALFWAY, age=10):
    return {"ts_epoch": now - age, "buckets": buckets}


def paced(**kw):
    """A policy entry for a paced folder, with the band knobs spelled out."""
    entry = {"paced": True, "model": "opus"}
    entry.update(kw)
    return entry


@pytest.fixture
def cwd(tmp_path):
    d = tmp_path / "work"
    d.mkdir()
    return norm_path(str(d))


class Clock:
    """A virtual clock that advances only when the code under test sleeps.

    Sleeps are recorded so a test can assert how the wait was divided, not just
    how long it lasted -- a band hold is supposed to be short, and "short"
    should mean the hook woke up and released, not that it never slept at all.
    """

    def __init__(self, start=HALFWAY, max_naps=500):
        self.t = start
        self.naps = []
        self.max_naps = max_naps

    def time(self):
        return self.t

    def sleep(self, seconds):
        self.naps.append(seconds)
        if len(self.naps) > self.max_naps:
            raise AssertionError("run() did not release; it is still holding")
        self.t += seconds

    @property
    def elapsed(self):
        return self.t - HALFWAY


class StillHolding(Exception):
    """The probe clock reached its target with run() still asleep."""


class Probe(Clock):
    """A clock that aborts the test once the hold outlives `stop_after`.

    Asserting "it never releases" cannot be done by waiting for a return, so
    invert it: run until the target and treat still being asleep as the pass.
    """

    def __init__(self, stop_after):
        Clock.__init__(self, max_naps=100_000)
        self.stop_after = stop_after

    def sleep(self, seconds):
        if self.elapsed >= self.stop_after:
            raise StillHolding
        Clock.sleep(self, seconds)


@pytest.fixture
def running(cwd, tmp_path, monkeypatch):
    """A paced folder with the clock, the snapshot and the log under control.

    Returns a callable taking the folder's policy entry and the buckets the
    snapshot carries, and giving back (clock, reason) from a full `hook.run`.
    Messages the hook logged are collected on `.logs`, because the verb it
    chooses is part of the behaviour: band holds are one per tool call and must
    not read like brakes.
    """
    policy_path = tmp_path / "policy.json"
    state_path = tmp_path / "state.json"
    monkeypatch.setattr(hook, "POLICY_PATH", str(policy_path))
    monkeypatch.setattr(hook, "STATE_PATH", str(state_path))
    # The snapshot ages as the virtual clock advances. Left alone it would cross
    # MAX_STALE mid-test and pull the refresh path in, which is a different
    # behaviour under test further down. Keep it fresh so these tests see only
    # the brake loop.
    monkeypatch.setattr(hook, "snapshot_age", lambda state, now: 10)
    monkeypatch.setattr(hook, "refresh_snapshot", lambda: False)
    messages = []
    monkeypatch.setattr(hook, "log", messages.append)

    def go(entry=None, buckets=None, defaults=None, clock=None):
        if buckets is None:
            buckets = {"session": session_bucket(HOT_PCT)}
        state_path.write_text(json.dumps(state_for(buckets)), encoding="utf-8")
        policy_path.write_text(json.dumps(policy_for(cwd, entry, defaults)),
                               encoding="utf-8")
        clk = clock or Clock()
        monkeypatch.setattr(hook.time, "time", clk.time)
        monkeypatch.setattr(hook.time, "sleep", clk.sleep)
        _brake_start, reason = hook.run(cwd)
        return clk, reason

    go.logs = messages
    return go


@pytest.fixture
def blind_running(cwd, tmp_path, monkeypatch):
    """The same, but with a snapshot old enough that the hook is flying blind.

    `snapshot_age` is left alone here -- the age is the point -- and every
    refresh fails, which is what "degraded" means: we cannot see, and cannot
    fix it by looking again.
    """
    policy_path = tmp_path / "policy.json"
    state_path = tmp_path / "state.json"
    monkeypatch.setattr(hook, "POLICY_PATH", str(policy_path))
    monkeypatch.setattr(hook, "STATE_PATH", str(state_path))
    monkeypatch.setattr(hook, "refresh_snapshot", lambda: False)
    monkeypatch.setattr(hook, "log", lambda msg: None)

    def go(entry=None, buckets=None, clock=None):
        if buckets is None:
            buckets = {"session": session_bucket(BAND_PCT)}
        state_path.write_text(
            json.dumps({"ts_epoch": HALFWAY - 9999, "buckets": buckets}),
            encoding="utf-8")
        policy_path.write_text(json.dumps(policy_for(cwd, entry)),
                               encoding="utf-8")
        clk = clock or Clock()
        monkeypatch.setattr(hook.time, "time", clk.time)
        monkeypatch.setattr(hook.time, "sleep", clk.sleep)
        _brake_start, reason = hook.run(cwd)
        return clk, reason

    return go


# --- geometry: where the throttle line sits ----------------------------------

def test_the_throttle_line_sits_a_band_under_the_brake_line():
    """The whole feature in one assertion: `low = allowed - band`.

    Mid-window the m0 floor is far below, so the band is at its full configured
    width and the two lines differ by exactly the configured percentage.
    """
    p = bucket_pace(session_bucket(FREE_PCT), HALFWAY, M0, M1, 6)
    assert p["allowed"] == ALLOWED_AT_HALFWAY
    assert p["low"] == ALLOWED_AT_HALFWAY - 6


def test_a_fresh_window_is_not_throttled_at_its_grubstake():
    """The m0 floor, and why `low` is a max() rather than a subtraction.

    m0 exists so a fresh window is startable at all -- the pure diagonal permits
    0% at 0% elapsed, so without the grubstake nothing could ever begin. A band
    subtracted from the line at f_t = 0 would sit below zero, and throttling the
    very first step out of a window that has just rolled would be perverse: the
    agent would be held at 0% usage. So the floor holds the throttle line at m0
    and the grubstake stays free at full speed.
    """
    fresh = bucket_pace(session_bucket(0), START, M0, M1, 6)
    assert fresh["allowed"] == M0
    assert fresh["low"] == M0             # not -1
    assert fresh["region"] == "free"

    # ...right up to the grubstake itself, which is spendable without a hold.
    assert bucket_pace(session_bucket(4), START, M0, M1, 6)["region"] == "free"


def test_the_band_opens_as_the_window_advances():
    """Early on the band is narrower than configured, and that is deliberate.

    While the brake line is still under m0 + band the floor is what binds, so
    the band's width is whatever the line has climbed so far. It reaches full
    width exactly when the brake line clears m0 + band -- at f_t = band / span
    -- and from there on the geometry is a plain parallel pair.
    """
    band = 6
    early = bucket_pace(session_bucket(10), at(0.02), M0, M1, band)
    assert early["low"] == M0
    assert early["allowed"] - early["low"] < band       # floored, not yet open

    crossover = bucket_pace(session_bucket(10), at(band / SPAN), M0, M1, band)
    assert crossover["allowed"] == pytest.approx(M0 + band)
    assert crossover["low"] == pytest.approx(M0)

    wide = bucket_pace(session_bucket(10), at(0.5), M0, M1, band)
    assert wide["allowed"] - wide["low"] == pytest.approx(band)


def test_band_zero_puts_both_lines_in_the_same_place():
    """The disabled case is not a special branch, it is the same arithmetic with
    a zero-width band -- which is why `band=0` can be trusted to be the old
    controller rather than a re-implementation of it."""
    p = bucket_pace(session_bucket(FREE_PCT), HALFWAY, M0, M1, 0)
    assert p["low"] == p["allowed"] == ALLOWED_AT_HALFWAY


def test_a_null_or_negative_band_is_treated_as_zero():
    """`band: null` is what the CLI writes to clear a defaults-level setting,
    and a hand-edited policy.json can carry anything at all. Neither may widen
    the band upwards or produce a throttle line above the brake line, so both
    coerce to the disabled case rather than being trusted."""
    zero = bucket_pace(session_bucket(BAND_PCT), HALFWAY, M0, M1, 0)
    assert bucket_pace(session_bucket(BAND_PCT), HALFWAY, M0, M1, None) == zero
    assert bucket_pace(session_bucket(BAND_PCT), HALFWAY, M0, M1, -5) == zero


def test_every_return_shape_carries_the_region_and_both_lines():
    """`status` and the hook both read these keys, on every path. A return that
    omitted one would not fail here in the arithmetic -- it would fail much
    later, as a KeyError inside a hook that is meant never to wedge a session.
    """
    keys = {"pct", "pess", "allowed", "low", "elapsed", "region", "over",
            "wait", "wake"}
    for p in (bucket_pace(session_bucket(FREE_PCT), HALFWAY, M0, M1, BAND),
              bucket_pace(session_bucket(BAND_PCT), HALFWAY, M0, M1, BAND),
              bucket_pace(session_bucket(HOT_PCT), HALFWAY, M0, M1, BAND),
              bucket_pace(floating_bucket(HOT_PCT), HALFWAY, M0, M1, BAND)):
        assert set(p) == keys


def test_a_bucket_with_no_percentage_is_still_unusable():
    """Unchanged, and the hook depends on it: None here is what makes it treat
    the bucket as the most restrictive region rather than the most convenient
    one."""
    assert bucket_pace({"pct": None}, HALFWAY, M0, M1, BAND) is None


# --- backward compatibility: band=0 is the old controller, exactly -----------

def old_bucket_pace(bucket, now, m0, m1):
    """The pre-band implementation, copied verbatim from the commit before it.

    This is the reference the compatibility tests below compare against. It is
    deliberately a copy and not an import: the point is to pin the numbers the
    old controller produced, so that if someone later "simplifies" the band
    arithmetic and the two disagree, this file says so. If this function ever
    has to be edited to make a test pass, that edit IS the regression.
    """
    pct = bucket.get("pct")
    if pct is None:
        return None
    span = 100 - m0 - m1
    if span <= 0:
        span = 1
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


def test_band_zero_reproduces_the_old_arithmetic_exactly():
    """The most important test in this file.

    The band is an addition to a controller people already run against a real
    budget, and `band` defaults to 0. So the default configuration has to be
    bit-for-bit the previous behaviour -- not "close enough", not "within a
    percent". Every key the old function returned must come back with the same
    value, over a grid that covers both ends of the window, both sides of the
    line, the clamp at the reset, and the nonsense configs the guard rails
    exist for (m0+m1 >= 100, and m0 = 0 where the grubstake vanishes).

    A drift here would not be visible as a failure anywhere else: it would be
    visible as a slightly different overnight bill.
    """
    for m0, m1 in ((M0, M1), (0, 0), (60, 60)):
        for f in (0.0, 0.1, 0.5, 0.75, 1.0):
            now = at(f)
            for pct in (0, 1, 40, 49, 50, 89, 95, 99, 100):
                b = session_bucket(pct)
                old = old_bucket_pace(b, now, m0, m1)
                new = bucket_pace(b, now, m0, m1, 0)
                where = f"m0={m0} m1={m1} f={f} pct={pct}"
                for key in old:
                    assert new[key] == old[key], f"{key} differs at {where}"


def test_band_zero_reproduces_the_old_no_reset_clause_path_exactly():
    """The same guarantee on the path where neither line can be drawn.

    A missing reset clause is not an exotic case -- it is what a window that has
    just rolled looks like -- so the fallback to the m0 floor has to survive the
    band unchanged too.
    """
    for m0, m1 in ((M0, M1), (0, 0), (60, 60)):
        for pct in (0, 4, 5, 50, 100):
            b = floating_bucket(pct)
            old = old_bucket_pace(b, HALFWAY, m0, m1)
            new = bucket_pace(b, HALFWAY, m0, m1, 0)
            for key in old:
                assert new[key] == old[key], f"{key} differs at m0={m0} pct={pct}"


def test_an_unconfigured_folder_gets_the_shipped_band(cwd):
    """`band` absent from the policy means the built-in default -- 7 points,
    held 180s per call -- and not some third thing. Compared as whole
    decisions, because the default reaching only half of them is exactly the
    sort of bug that shows up weeks later as an unexplained hold.

    This is what a folder paced with no band flags actually runs, so it is the
    configuration most installs are in."""
    state = state_for({"session": session_bucket(HOT_PCT)})
    absent = hook.decide(policy_for(cwd), state, cwd, HALFWAY)
    shipped = paced(band=DEFAULT_BAND, band_delay=DEFAULT_BAND_DELAY)
    explicit = hook.decide(policy_for(cwd, shipped), state, cwd, HALFWAY)
    assert absent == explicit
    assert absent["region"] == "over"
    assert absent["band_delay"] == DEFAULT_BAND_DELAY


def test_a_written_null_turns_a_knob_off_against_a_default_that_is_on(cwd):
    """`--no-band-delay` writes `band_delay: null`, and that has to mean OFF.

    Falling back on a null -- which is what a plain coerce_num does -- was
    harmless while the built-in default was None itself, and became a silent
    reversal the moment the default became 180: the folder that asked for a
    free run would have been put in the lower gear by the very flag that turns
    it off. Same for a null `band`, which must read as zero width rather than
    as the shipped 7."""
    state = state_for({"session": session_bucket(BAND_PCT)})
    free = hook.decide(policy_for(cwd, paced(band=BAND, band_delay=None)),
                       state, cwd, HALFWAY)
    assert free["band_delay"] is None
    assert free["braked"] is False

    unbanded = hook.decide(policy_for(cwd, paced(band=None)), state, cwd,
                           HALFWAY)
    assert unbanded["region"] == "free"


def test_decide_brakes_to_the_old_wake_and_says_what_it_always_said(cwd):
    """End to end at the hook's own level: with the band off, the folder is held
    to the same instant and for the same stated reason as before. `hook.log` is
    the only record of what the pacer did overnight, so the reason line is part
    of the contract, not decoration."""
    old = old_bucket_pace(session_bucket(HOT_PCT), HALFWAY, M0, M1)
    d = hook.decide(policy_for(cwd, paced(band=0)),
                    state_for({"session": session_bucket(HOT_PCT)}),
                    cwd, HALFWAY)
    assert d["braked"] is True
    assert d["wake_at"] == old["wake"]
    assert d["reason"] == f"session {HOT_PCT}% over line {old['allowed']:.1f}%"


def test_with_the_band_off_there_is_no_band_region(cwd):
    """The three regions collapse back to two, so nothing downstream -- the
    `throttle` log verb, the near-line refresh, `status` -- can fire on a folder
    that never asked for the feature."""
    for pct in (FREE_PCT, BAND_PCT, HOT_PCT):
        d = hook.decide(policy_for(cwd, paced(band=0)),
                        state_for({"session": session_bucket(pct)}),
                        cwd, HALFWAY)
        assert d["region"] in ("free", "over")


# --- region classification, including the exact edges ------------------------
#
# The brake line has to land on a whole number for the equality edges to be
# assertable without float fuzz. m0=5, m1=9 gives span 86, so at the halfway
# point the line stands at exactly 5 + 43 = 48.0 and a band of 3 puts the
# throttle line at exactly 45.0. Both comparisons are then exact, and `pess` is
# a whole number as /usage reports it.

EDGE_M1 = 9
EDGE_BAND = 3
EDGE_ALLOWED = 48.0
EDGE_LOW = 45.0


def edge_pace(pct):
    return bucket_pace(session_bucket(pct), HALFWAY, M0, EDGE_M1, EDGE_BAND)


def test_the_edge_geometry_is_exact():
    """Guard for the tests below: if this drifts they stop testing the edges and
    start testing floating point."""
    p = edge_pace(0)
    assert p["allowed"] == EDGE_ALLOWED
    assert p["low"] == EDGE_LOW


@pytest.mark.parametrize("pct,pess,region", [
    (43, 44, "free"),   # under the throttle line
    (44, 45, "free"),   # exactly ON it -- inclusive, see below
    (45, 46, "band"),   # between the lines
    (47, 48, "band"),   # exactly ON the brake line -- also inclusive
    (48, 49, "over"),   # above it
])
def test_the_region_is_judged_on_pess_at_each_boundary(pct, pess, region):
    """Regions are judged on `pess = pct + 1`, not on `pct`.

    /usage reports whole percents, so a reported P could really be anything up
    to P+1; the controller rounds against itself. Both comparisons are
    inclusive at the lower end of the region above -- `pess == low` is still
    free and `pess == allowed` is still band -- which keeps the brake line's
    promise ("never ABOVE this") literally true and stops a bucket sitting
    exactly on a line from oscillating between two regions on float noise.
    """
    p = edge_pace(pct)
    assert p["pess"] == pess
    assert p["region"] == region


def test_over_still_means_above_the_brake_line():
    """`over` kept its old name because it kept its old meaning.

    A bucket in the band is being throttled but is NOT over the line, and
    everything that reads this flag -- `status`, the daemon's reporting, any
    eyeball on a log -- depends on that distinction. Reporting a band as over
    would turn the band into a permanent state of alarm.
    """
    assert edge_pace(45)["region"] == "band"
    assert edge_pace(45)["over"] is False
    assert edge_pace(48)["over"] is True
    for pct in (43, 44, 45, 47, 48):
        p = edge_pace(pct)
        assert p["over"] == (p["region"] == "over")


def test_free_buckets_carry_no_wait():
    """Nothing below the throttle line has anything to wait for, and the absence
    of a wake is what lets the hook skip the bucket entirely."""
    p = edge_pace(43)
    assert p["wait"] is None and p["wake"] is None


def test_both_braking_regions_carry_a_wait():
    """A band hold is a real hold with a real target, not a flag. It is capped
    in `run` rather than being uncomputed here, so that `status` can say when
    the throttle would clear even while a band_delay is what actually ends each
    individual hold."""
    for pct in (45, 48):
        p = edge_pace(pct)
        assert p["wake"] is not None
        assert p["wait"] == pytest.approx(p["wake"] - HALFWAY)


# --- hysteresis: both holds aim at the throttle line -------------------------

def test_an_over_hold_solves_for_the_throttle_line_not_the_brake_line():
    """The hysteresis, and the reason the feature exists.

    The old hold ended when the BRAKE line reached `pess`, which handed the
    agent back under one quantum of headroom -- so the next 1% tick put it over
    again and it paid another full hold, and another cold prompt cache. Aiming
    at the throttle line instead means the release comes when the brake line has
    climbed a whole band past `pess`, and the agent resumes with a band's worth
    of running in front of it.
    """
    solved = bucket_pace(session_bucket(49), HALFWAY, M0, M1, BAND)["wake"]
    assert solved == pytest.approx(START + ((50 + BAND - M0) / SPAN) * SESSION_WINDOW)

    # The old target -- the brake line reaching pess -- is passed on the way.
    assert solved > old_bucket_pace(session_bucket(49), HALFWAY, M0, M1)["wake"]


def test_the_hold_is_longer_by_exactly_band_over_slope():
    """The price of the hysteresis, stated in the units it is paid in.

    The line rises span/window percent per second, so buying `band` points of
    headroom costs band * window / span seconds -- 1034s on the 5h window here,
    and about 1.94h per point on the weekly one. That number is why the default
    band is small and why the docs warn about the 6h hook timeout: a band wide
    enough to imply a hold longer than the registered timeout gets the hook
    killed, and a killed hook means the agent proceeds UNPACED.
    """
    plain = bucket_pace(session_bucket(49), HALFWAY, M0, M1, 0)["wake"]
    banded = bucket_pace(session_bucket(49), HALFWAY, M0, M1, BAND)["wake"]
    assert banded - plain == pytest.approx(BAND * SESSION_WINDOW / SPAN)


def test_the_release_leaves_a_full_band_of_headroom():
    """What the agent actually gets back at the end of a hold.

    Under one line it resumed sitting ON the line. Now it resumes a full band
    beneath it: the throttle line has just caught `pess`, so the brake line --
    the one that means "never above this" -- stands `band` points higher.
    """
    p = bucket_pace(session_bucket(49), HALFWAY, M0, M1, BAND)
    arrived = bucket_pace(session_bucket(49), p["wake"], M0, M1, BAND)
    assert arrived["low"] == pytest.approx(arrived["pess"])
    assert arrived["allowed"] - arrived["pess"] == pytest.approx(BAND)

    # And a moment later it is unambiguously free -- the hold is over, rather
    # than ending exactly on a boundary it could fall back across.
    assert bucket_pace(session_bucket(49), p["wake"] + 1, M0, M1,
                       BAND)["region"] == "free"


def test_a_band_hold_aims_at_the_same_place_as_an_over_hold():
    """One target, two regions. The brake line does not select where the hold
    ends, only whether it is allowed to be cut short -- which is what makes the
    two knobs composable instead of two competing control laws."""
    banded = bucket_pace(session_bucket(BAND_PCT), HALFWAY, M0, M1, BAND)
    assert banded["region"] == "band"
    arrived = bucket_pace(session_bucket(BAND_PCT), banded["wake"], M0, M1, BAND)
    assert arrived["low"] == pytest.approx(arrived["pess"])


def test_the_wake_is_still_clamped_to_the_window_reset():
    """The band must not push a hold past the reset that would have ended it.

    The window rolling zeroes usage, so waiting beyond it buys nothing and costs
    everything. This is the case the clamp exists for: without the band the
    solve lands inside the window, with it the solve lands outside, and the
    answer has to be the reset either way.
    """
    plain = bucket_pace(session_bucket(89), HALFWAY, M0, M1, 0)["wake"]
    banded = bucket_pace(session_bucket(89), HALFWAY, M0, M1, BAND)["wake"]
    assert plain < RESETS
    assert banded == RESETS


# --- band_delay: the band is free-running unless you say otherwise -----------

def test_a_band_with_no_band_delay_does_not_brake(cwd):
    """`band_delay` unset means the band is pure release hysteresis.

    It deepens the hold that the brake line triggers; it is not itself a reason
    to stop. So a folder in the band with no band_delay runs at full speed and
    only the brake line stops it -- which is the whole point of the two knobs
    being orthogonal: you can have the hysteresis without paying a per-call
    tax.
    """
    d = hook.decide(policy_for(cwd, paced(band=BAND, band_delay=None)),
                    state_for({"session": session_bucket(BAND_PCT)}),
                    cwd, HALFWAY)
    assert d["braked"] is False


def test_a_free_running_band_still_reports_its_region(cwd):
    """...and it must still say it is in the band, even though it did not brake.

    `region` is GEOMETRIC -- where we stand, not what we do about it -- and
    `run` reads it to decide how fresh the snapshot has to be before it dares
    let a step through. Collapsing "not braking" into region "free" would
    silently switch the near-line refresh off exactly where it matters, and the
    agent would sail past the brake line on data it never re-read.
    """
    d = hook.decide(policy_for(cwd, paced(band=BAND, band_delay=None)),
                    state_for({"session": session_bucket(BAND_PCT)}),
                    cwd, HALFWAY)
    assert d["region"] == "band"
    assert d["blind"] is False


def test_the_near_line_refresh_fires_on_a_free_running_band(cwd, tmp_path,
                                                            monkeypatch):
    """The phasing story, end to end: not braking is not the same as not looking.

    Above the throttle line the agent is about to take a step -- immediately
    here, since band_delay is unset -- and one step can burn a lot. A snapshot
    up to MAX_STALE = 180s old is fine when we are well clear; it is not fine
    when the next step could cross the brake line unseen. So the first pass of
    each invocation demands one no older than NEAR_STALE. `/usage` costs no
    tokens, so this is wall clock only, and only where it matters.
    """
    policy_path = tmp_path / "policy.json"
    state_path = tmp_path / "state.json"
    state_path.write_text(json.dumps(state_for({"session":
                                                session_bucket(BAND_PCT)})),
                          encoding="utf-8")
    monkeypatch.setattr(hook, "POLICY_PATH", str(policy_path))
    monkeypatch.setattr(hook, "STATE_PATH", str(state_path))
    monkeypatch.setattr(hook, "log", lambda msg: None)
    # Older than NEAR_STALE (15s), comfortably younger than MAX_STALE (180s):
    # the age at which the region is the only thing that decides.
    monkeypatch.setattr(hook, "snapshot_age", lambda state, now: 60)
    calls = []
    monkeypatch.setattr(hook, "refresh_snapshot",
                        lambda: (calls.append(1), True)[1])
    clock = Clock()
    monkeypatch.setattr(hook.time, "time", clock.time)
    monkeypatch.setattr(hook.time, "sleep", clock.sleep)

    policy_path.write_text(
        json.dumps(policy_for(cwd, paced(band=BAND, band_delay=None))),
        encoding="utf-8")
    _brake_start, reason = hook.run(cwd)
    assert reason == "line-caught-up"        # it did not brake...
    assert len(calls) == 1                   # ...but it did look again


def test_a_free_bucket_trusts_a_sixty_second_old_snapshot(cwd, tmp_path,
                                                          monkeypatch):
    """The contrast case, and the reason the refresh is conditional at all.

    Well under the throttle line there is nothing about to happen that a
    three-minute-old reading could hide, and a refresh costs a couple of seconds
    of wall clock on a hook that runs before every tool call in every agent.
    Paying that unconditionally is how you make the pacer the slowest thing in
    the session.
    """
    policy_path = tmp_path / "policy.json"
    state_path = tmp_path / "state.json"
    state_path.write_text(json.dumps(state_for({"session":
                                                session_bucket(FREE_PCT)})),
                          encoding="utf-8")
    monkeypatch.setattr(hook, "POLICY_PATH", str(policy_path))
    monkeypatch.setattr(hook, "STATE_PATH", str(state_path))
    monkeypatch.setattr(hook, "log", lambda msg: None)
    monkeypatch.setattr(hook, "snapshot_age", lambda state, now: 60)
    calls = []
    monkeypatch.setattr(hook, "refresh_snapshot",
                        lambda: (calls.append(1), True)[1])
    clock = Clock()
    monkeypatch.setattr(hook.time, "time", clock.time)
    monkeypatch.setattr(hook.time, "sleep", clock.sleep)

    policy_path.write_text(json.dumps(policy_for(cwd, paced(band=BAND))),
                           encoding="utf-8")
    _brake_start, reason = hook.run(cwd)
    assert reason == "line-caught-up"
    assert calls == []


def test_band_delay_turns_the_band_into_a_lower_gear(cwd):
    """Set it and the band stops being a pass-through: each tool call holds this
    long, so the agent burns slowly and stays cache-warm instead of alternating
    long dark holds with full-speed bursts."""
    d = hook.decide(policy_for(cwd, paced(band=BAND, band_delay=20)),
                    state_for({"session": session_bucket(BAND_PCT)}),
                    cwd, HALFWAY)
    assert d["braked"] is True
    assert d["region"] == "band"
    assert d["band_delay"] == 20


def test_a_band_hold_names_both_lines(cwd):
    """The reason line has to answer "why am I stopped?" without the reader
    having to recompute the geometry. A band hold is the one that will appear
    most often, and "over line 48.5%" would be a lie in it -- we are under the
    brake line, held by the other one."""
    d = hook.decide(policy_for(cwd, paced(band=BAND, band_delay=20)),
                    state_for({"session": session_bucket(BAND_PCT)}),
                    cwd, HALFWAY)
    assert d["reason"] == (f"session {BAND_PCT}% in band (line 48.5%, "
                           "throttle 43.5%)")


def test_a_band_hold_wakes_at_the_solved_release(cwd):
    """Even though band_delay is what ends each individual hold, the wake it
    counts towards is the real one -- so a band_delay longer than the remaining
    wait releases when the line catches up rather than sleeping past it."""
    p = bucket_pace(session_bucket(BAND_PCT), HALFWAY, M0, M1, BAND)
    d = hook.decide(policy_for(cwd, paced(band=BAND, band_delay=20)),
                    state_for({"session": session_bucket(BAND_PCT)}),
                    cwd, HALFWAY)
    assert d["wake_at"] == p["wake"]


def test_free_is_free_whatever_the_band_delay_says(cwd):
    """Below the throttle line nothing holds. A band_delay that applied
    everywhere would be a global per-tool-call tax, which is a completely
    different (and much worse) tool."""
    d = hook.decide(policy_for(cwd, paced(band=BAND, band_delay=20)),
                    state_for({"session": session_bucket(FREE_PCT)}),
                    cwd, HALFWAY)
    assert d["braked"] is False
    assert d["region"] == "free"


def test_the_band_knob_reaches_the_geometry(cwd):
    """The same snapshot, the same line, two different verdicts -- so the
    setting is genuinely plumbed through to `bucket_pace` rather than only
    surfaced in the returned dict."""
    state = state_for({"session": session_bucket(BAND_PCT)})
    off = hook.decide(policy_for(cwd, paced(band=0)), state, cwd, HALFWAY)
    on = hook.decide(policy_for(cwd, paced(band=BAND, band_delay=20)), state,
                     cwd, HALFWAY)
    assert off["region"] == "free" and off["braked"] is False
    assert on["region"] == "band" and on["braked"] is True


def test_the_defaults_supply_both_knobs_and_the_entry_overrides_them(cwd):
    """Same resolution order as every other knob: folder entry, then defaults,
    then the built-in. A band configured globally has to reach a folder that
    only asked to be paced, or turning it on would mean editing every entry."""
    defaults = {"m0": M0, "m1": M1, "chunk": 15, "band": BAND, "band_delay": 30}
    state = state_for({"session": session_bucket(BAND_PCT)})

    inherited = hook.decide(policy_for(cwd, defaults=defaults), state, cwd,
                            HALFWAY)
    assert inherited["region"] == "band"
    assert inherited["band_delay"] == 30

    overridden = hook.decide(policy_for(cwd, paced(band_delay=5),
                                        defaults=defaults), state, cwd, HALFWAY)
    assert overridden["band_delay"] == 5

    # And an entry-level band of 0 turns the feature off for this folder alone.
    off = hook.decide(policy_for(cwd, paced(band=0), defaults=defaults), state,
                      cwd, HALFWAY)
    assert off["region"] == "free"


def test_band_delay_is_surfaced_on_every_braking_path(cwd):
    """`run` reads it off the decision each cycle, so any path that can brake
    has to carry it -- including the ones where it will not be used. A missing
    key there would read as "no cap" on a path that wanted one."""
    entry = paced(band=BAND, band_delay=20)
    over = hook.decide(policy_for(cwd, entry),
                       state_for({"session": session_bucket(HOT_PCT)}), cwd,
                       HALFWAY)
    blind = hook.decide(policy_for(cwd, entry), state_for({}), cwd, HALFWAY,
                        degraded=True)
    empty = hook.decide(policy_for(cwd, entry), {"buckets": {}}, cwd, HALFWAY)
    for d in (over, blind, empty):
        assert d["band_delay"] == 20


# --- run: what one hold costs ------------------------------------------------

def test_a_free_running_band_never_sleeps(running):
    """The band with no band_delay costs nothing at all -- not a short hold, not
    a chunk, nothing. Anything else would be a per-tool-call tax on a folder
    that only asked for release hysteresis."""
    clock, reason = running(entry=paced(band=BAND, band_delay=None),
                            buckets={"session": session_bucket(BAND_PCT)})
    assert reason == "line-caught-up"
    assert clock.naps == []


def test_a_band_hold_lasts_band_delay(running):
    """One hold per tool call, and a short one: the agent is safely under the
    brake line, so ending the hold early gives up no restraint at all. It just
    means burning in a lower gear rather than stopping dead."""
    clock, reason = running(entry=paced(band=BAND, band_delay=20),
                            buckets={"session": session_bucket(BAND_PCT)})
    assert reason == "band-release"
    assert clock.elapsed == pytest.approx(20, abs=1)


def test_a_band_hold_is_still_chunked(running):
    """Chunking is what lets a policy edit reach an already-frozen agent, and
    the band cap must not collapse the hold into one uninterruptible sleep."""
    clock, _reason = running(entry=paced(band=BAND, band_delay=20),
                             buckets={"session": session_bucket(BAND_PCT)})
    assert max(clock.naps) <= 15
    assert clock.elapsed == pytest.approx(20, abs=1)


def test_a_band_hold_logs_throttle_rather_than_brake(running):
    """Band holds are one per tool call, so they would swamp hook.log.

    Worse, they would drown the invariant that reads it: a `brake` with no
    matching `release` is the only visible sign that the harness killed the hook
    at its registered timeout. `grep ' brake '` has to keep counting real
    brakes, so a throttle gets its own verb.
    """
    running(entry=paced(band=BAND, band_delay=20),
            buckets={"session": session_bucket(BAND_PCT)})
    held = [m for m in running.logs if m.startswith(("throttle", "brake"))]
    assert len(held) == 1
    assert held[0].startswith("throttle")


def test_an_over_hold_still_logs_a_brake(running):
    """The contrast: above the brake line nothing changed, and the log has to go
    on saying so."""
    running(entry=paced(band=BAND, band_delay=20, max_delay=10))
    held = [m for m in running.logs if m.startswith(("throttle", "brake"))]
    assert len(held) == 1
    assert held[0].startswith("brake")


def test_band_delay_and_max_delay_take_the_smaller(running):
    """Both caps apply in the band, and the tighter one wins.

    They are capping the same thing -- one hold -- so the only sane combination
    is the minimum. The order they are configured in must not matter.
    """
    short_band, _r = running(entry=paced(band=BAND, band_delay=10,
                                         max_delay=30),
                             buckets={"session": session_bucket(BAND_PCT)})
    assert short_band.elapsed == pytest.approx(10, abs=1)

    short_cap, _r = running(entry=paced(band=BAND, band_delay=30, max_delay=10),
                            buckets={"session": session_bucket(BAND_PCT)})
    assert short_cap.elapsed == pytest.approx(10, abs=1)


def test_max_delay_alone_does_not_cut_a_band_hold_short(running):
    """With no band_delay the band is free-running, so there is no hold for
    max_delay to cap -- the agent never stopped. max_delay's meaning is
    unchanged and it has nothing to do here."""
    clock, reason = running(entry=paced(band=BAND, band_delay=None,
                                        max_delay=10),
                            buckets={"session": session_bucket(BAND_PCT)})
    assert reason == "line-caught-up"
    assert clock.naps == []


def test_max_delay_still_caps_an_over_hold_exactly_as_before(running):
    """Above the brake line the band changes nothing about the cap. This is the
    one knob that deliberately proceeds while over the line, it is opt-in, and
    adding a second line below must not have quietly changed what it means."""
    clock, reason = running(entry=paced(band=BAND, band_delay=5, max_delay=40))
    assert reason == "max_delay-release"
    assert clock.elapsed == pytest.approx(40, abs=1)


def test_band_delay_cannot_release_an_over_hold(running):
    """The guarantee, stated as the thing that must not happen.

    band_delay is cheap precisely because it only ever applies below the brake
    line. If it also capped holds above it, the ceiling would be gone: every
    folder with a band_delay would proceed over the line every few seconds,
    which is the one thing max_delay is documented (and opt-in) for.
    """
    with pytest.raises(StillHolding):
        running(entry=paced(band=BAND, band_delay=5), clock=Probe(stop_after=600))


def test_without_a_band_delay_an_over_hold_is_unchanged(running):
    """And with neither cap set it holds until the line catches up, exactly as
    it did before the band existed."""
    with pytest.raises(StillHolding):
        running(entry=paced(band=BAND), clock=Probe(stop_after=600))


# --- blind maps to over, deliberately ----------------------------------------
#
# The last three of these once failed: `decide` returned a band decision before
# it ever reached the degraded branch, so a stale snapshot that happened to read
# "in band" was released by band_delay. That made band_delay a second knob that
# proceeds while blind -- the property max_delay is documented, opt-in and
# deliberately lonely for. Fixed by letting a banded-but-degraded read fall
# through to the blind branch; these pin it shut.

def test_a_degraded_snapshot_under_the_line_still_brakes(cwd):
    """The half of the doctrine that does work, and the baseline for the rest.

    A stale reading that says "under the line" is not evidence of headroom --
    only that we had headroom some minutes ago -- so it holds anyway and says
    why, distinctly. "Braked because blind" and "braked because hot" are
    completely different problems at 3am.
    """
    d = hook.decide(policy_for(cwd, paced(band=BAND, band_delay=5)),
                    state_for({"session": session_bucket(FREE_PCT)}, age=9999),
                    cwd, HALFWAY, degraded=True)
    assert d["braked"] is True
    assert d["blind"] is True
    assert d["region"] == "over"
    assert d["reason"].startswith("BLIND:")


def test_a_degraded_snapshot_over_the_line_is_acted_on_with_confidence(cwd):
    """The other half, and the asymmetry that makes it sound.

    Consumption only ever rises within a window, so a stale reading that is
    already over the line is a floor, not a guess: it can only have got worse.
    That hold is a real brake, not a blind one, and is labelled accordingly.
    """
    d = hook.decide(policy_for(cwd, paced(band=BAND, band_delay=5)),
                    state_for({"session": session_bucket(HOT_PCT)}, age=9999),
                    cwd, HALFWAY, degraded=True)
    assert d["braked"] is True
    assert d["blind"] is False
    assert d["region"] == "over"


def test_a_degraded_snapshot_reports_over_even_when_it_looks_like_a_band(cwd):
    """"I cannot see" must not resolve to "I am comfortably under the line".

    Stale data is not useless -- usage only rises within a window, so an old
    reading is a lower bound -- and the doctrine that falls out of that
    asymmetry is that stale data can justify BRAKING but never ALLOWING.
    Reporting a blind hook as "in band" would assert the one thing it does not
    know: that it is still below the brake line. So blind reads as `over`, the
    most restrictive region, whatever the last snapshot happened to say.
    """
    d = hook.decide(policy_for(cwd, paced(band=BAND, band_delay=5)),
                    state_for({"session": session_bucket(BAND_PCT)}, age=9999),
                    cwd, HALFWAY, degraded=True)
    assert d["braked"] is True
    assert d["blind"] is True
    assert d["region"] == "over"


def test_band_delay_does_not_release_a_blind_agent(blind_running):
    """The consequence, and the reason the region above is not a detail.

    If band_delay released a blind hold, then a folder with a band_delay and a
    dead daemon would step every few seconds with no idea where it stood --
    turning the cheapest knob in the tool into a silent bypass of the ceiling.
    Only max_delay frees a blind agent, exactly as before the band existed.

    Today it does exactly that, whenever the last snapshot before the daemon
    died happened to read "in band": `decide` returns the band decision before
    the degraded branch is reached. A snapshot that says "free" is caught (see
    above), so the hole is only open in the region where it is most expensive.
    """
    with pytest.raises(StillHolding):
        blind_running(entry=paced(band=BAND, band_delay=5),
                      clock=Probe(stop_after=600))


def test_max_delay_still_releases_a_blind_agent_and_says_so(blind_running):
    """Proceeding while blind is the loosest thing this tool ever does, so it
    stays opt-in and the log goes on shouting about it.

    With the band misclassification above, this release comes after band_delay
    instead of max_delay and is logged as an ordinary band release -- so the
    one event the log exists to make loud becomes the quietest line in it.
    """
    clock, reason = blind_running(entry=paced(band=BAND, band_delay=5,
                                              max_delay=30))
    assert reason == "max_delay-release-WHILE-BLIND"
    assert clock.elapsed == pytest.approx(30, abs=1)


def test_a_snapshot_with_no_usable_buckets_reports_over_too(cwd):
    """Same reasoning one step earlier: a snapshot that carries nothing this
    folder is paced against is not evidence of headroom either."""
    d = hook.decide(policy_for(cwd, paced(band=BAND, band_delay=5)),
                    {"ts_epoch": HALFWAY - 10, "buckets": {}}, cwd, HALFWAY)
    assert d["braked"] is True
    assert d["region"] == "over"
    assert d["blind"] is True


def test_an_unusable_bucket_reads_as_over_not_as_a_band(cwd):
    """A bucket carrying no percentage tells us nothing about where it stands,
    and "nothing" resolves to the most restrictive region, not the most
    convenient one. Letting band_delay release it would be the same mistake as
    letting it release a blind hook."""
    d = hook.decide(policy_for(cwd, paced(band=BAND, band_delay=5)),
                    state_for({"session": {"pct": None}}), cwd, HALFWAY)
    assert d["region"] == "over"
    assert "unusable" in d["reason"]
    assert d["wake_at"] == HALFWAY + 15


# --- several buckets ---------------------------------------------------------

def test_the_most_restrictive_region_wins(cwd):
    """One bucket over the brake line and another merely in the band is an
    `over` decision. Regions combine like the wake does -- worst case -- because
    the budget they protect is shared: being comfortable on one line is no
    consolation for being over another."""
    d = hook.decide(policy_for(cwd, paced(band=BAND, band_delay=10)),
                    state_for({"session": session_bucket(HOT_PCT),
                               "week:all models": week_bucket(38)}),
                    cwd, HALFWAY)
    assert d["region"] == "over"
    assert "session" in d["reason"] and "week:all models" in d["reason"]
    assert "in band" in d["reason"]      # ...and it still says which is which


def test_the_wake_is_the_latest_OVER_bucket_and_a_band_bucket_does_not_extend_it(cwd):
    """The max runs across the `over` buckets only. A band bucket names itself
    in the reason but does not lengthen the hold.

    The two regions demand different things, and pooling their wakes confuses
    the two. An `over` bucket demands a hold until its throttle line catches up,
    which is hours. A `band` bucket demands one `band_delay` -- and with
    `band_delay` unset, nothing at all. Taking the max across both would convert
    "the weekly line wants ten seconds a step" into "hold for 5.8 hours", which
    is not a stricter reading of the same demand but a different demand
    altogether.

    Releasing at the session's wake does hand back an agent that is still inside
    the weekly band, and that is correct: it takes one step and the next
    PreToolUse throttles it. Being throttled is not being stopped, and the whole
    point of the band is that travelling through it is cheap.

    The failure mode this guards is not merely wasted time. The weekly line
    rises ~0.52%/h, so every band point is ~1.94h of pooled wake, and a hold
    pushed past the hook's registered timeout gets the hook KILLED -- after
    which the agent proceeds unpaced, silently. So the bug would spend the
    guarantee to buy nothing. (That ceiling is HOOK_TIMEOUT, since raised to
    48h; the point stands, it just takes more pooling to reach.)
    """
    week = bucket_pace(week_bucket(38), HALFWAY, M0, M1, BAND)
    session = bucket_pace(session_bucket(HOT_PCT), HALFWAY, M0, M1, BAND)
    assert week["region"] == "band" and session["region"] == "over"
    assert week["wake"] > session["wake"]

    d = hook.decide(policy_for(cwd, paced(band=BAND, band_delay=10)),
                    state_for({"session": session_bucket(HOT_PCT),
                               "week:all models": week_bucket(38)}),
                    cwd, HALFWAY)
    assert d["wake_at"] == session["wake"]
    # ...but the weekly bucket is still named, so hook.log says why both lines
    # were consulted rather than leaving the band bucket invisible.
    assert "week:all models" in d["reason"] and "in band" in d["reason"]


def test_the_wake_is_the_latest_of_several_over_buckets(cwd):
    """The contrast that gives the test above its meaning: among buckets that
    ARE over, the max still rules. Releasing at the earlier of two real brakes
    would hand back an agent that is still over the other line -- the weekly
    window is seven days long, so its line rises fifty-six times more slowly and
    it is very often the one that binds."""
    week = bucket_pace(week_bucket(60), HALFWAY, M0, M1, BAND)
    session = bucket_pace(session_bucket(HOT_PCT), HALFWAY, M0, M1, BAND)
    assert week["region"] == "over" and session["region"] == "over"
    assert week["wake"] > session["wake"]

    d = hook.decide(policy_for(cwd, paced(band=BAND, band_delay=10)),
                    state_for({"session": session_bucket(HOT_PCT),
                               "week:all models": week_bucket(60)}),
                    cwd, HALFWAY)
    assert d["wake_at"] == week["wake"]


def test_a_free_bucket_contributes_nothing(cwd):
    """The contrast that gives the test above its meaning: only hot buckets are
    in the max. A bucket under its throttle line is not waiting for anything, so
    it must not drag the wake around."""
    d = hook.decide(policy_for(cwd, paced(band=BAND, band_delay=10)),
                    state_for({"session": session_bucket(HOT_PCT),
                               "week:all models": week_bucket(10)}),
                    cwd, HALFWAY)
    assert "week" not in d["reason"]
    assert d["wake_at"] == bucket_pace(session_bucket(HOT_PCT), HALFWAY, M0, M1,
                                       BAND)["wake"]


def test_two_band_buckets_stay_a_band(cwd):
    """Several buckets in the band do not add up to being over the line. The
    regions are geometric facts about separate windows, not a score."""
    d = hook.decide(policy_for(cwd, paced(band=BAND, band_delay=10)),
                    state_for({"session": session_bucket(BAND_PCT),
                               "week:all models": week_bucket(38)}),
                    cwd, HALFWAY)
    assert d["region"] == "band"
    assert d["braked"] is True


def test_several_free_running_bands_still_do_not_brake(cwd):
    """...and with no band_delay, neither of them stops anything -- while both
    are still reported, so the near-line refresh sees them."""
    d = hook.decide(policy_for(cwd, paced(band=BAND, band_delay=None)),
                    state_for({"session": session_bucket(BAND_PCT),
                               "week:all models": week_bucket(38)}),
                    cwd, HALFWAY)
    assert d["region"] == "band"
    assert d["braked"] is False


# --- the no-reset-clause path ------------------------------------------------

def test_without_a_reset_clause_both_lines_collapse_onto_m0():
    """No reset clause means f_t is unknown, so neither line can be drawn.

    The fallback judges against m0, which is safe because the brake line never
    dips below it. There is nowhere to put a throttle line under a floor that is
    already the most conservative thing we can say, so both lines report m0 and
    the band -- however wide it is configured -- has no room to exist.
    """
    p = bucket_pace(floating_bucket(HOT_PCT), HALFWAY, M0, M1, 20)
    assert p["allowed"] == M0
    assert p["low"] == M0
    assert p["elapsed"] is None


def test_there_is_no_band_region_without_a_reset_clause():
    """Either the grubstake covers you or you are over it -- two regions, not
    three. A band region here would be a claim about a line we cannot draw, and
    it would be released by band_delay on the strength of that claim."""
    assert bucket_pace(floating_bucket(4), HALFWAY, M0, M1, 20)["region"] == "free"
    for pct in (5, 50, 99):
        p = bucket_pace(floating_bucket(pct), HALFWAY, M0, M1, 20)
        assert p["region"] == "over"
        assert p["over"] is True


def test_an_unsolvable_hold_has_no_wake_to_aim_at():
    """Being over an unknown line cannot be solved for a release time, so the
    hook re-checks on the chunk until the clause reappears rather than inventing
    a wake."""
    p = bucket_pace(floating_bucket(HOT_PCT), HALFWAY, M0, M1, 20)
    assert p["wait"] is None and p["wake"] is None


def test_decide_holds_on_the_chunk_over_the_floor(cwd):
    """At the hook's level the unsolvable hold is a chunked re-check, and it
    reports the region it is really in -- `over` -- so nothing downstream treats
    it as a throttle."""
    d = hook.decide(policy_for(cwd, paced(band=20, band_delay=5)),
                    state_for({"session": floating_bucket(HOT_PCT)}), cwd,
                    HALFWAY)
    assert d["braked"] is True
    assert d["region"] == "over"
    assert d["wake_at"] == HALFWAY + 15
    assert d["reason"] == f"session {HOT_PCT}% over floor {M0}%"


def test_band_delay_cannot_release_a_hold_over_the_floor(running):
    """The doctrine again, on the path most likely to hit it in practice: a
    window that has just rolled and is not yet reporting a reset clause. We are
    over the only line we can draw, so only max_delay may cut this short."""
    with pytest.raises(StillHolding):
        running(entry=paced(band=20, band_delay=5),
                buckets={"session": floating_bucket(HOT_PCT)},
                clock=Probe(stop_after=300))


# --- the hold runs DOWN, which is the whole feature -------------------------
#
# These are the tests that were missing, and their absence is why the band
# shipped as a no-op in an earlier draft. Every other test here samples one
# instant, or starts the loop already inside the band. `decide` is stateless
# and reclassifies from scratch, so a bucket held above the brake line stops
# being `over` the moment the line rises past `pess` -- the old, zero-headroom
# release point. A test that never runs the clock across that crossing cannot
# see the difference between a band that works and a band that is decoration.
#
# CROSSING_PCT sits just over the brake line so the two crossings are far
# apart and distinguishable: the brake line reaches pess in ~103s, the throttle
# line in ~1138s. HOT_PCT is no good here -- it is so far over that its wake
# clamps to the window's own reset, which would hide the difference.

CROSSING_PCT = 48       # pess 49, just above the 48.5% brake line at f_t=0.5


def _crossings():
    """(brake-line crossing, throttle-line crossing) for CROSSING_PCT, as
    offsets from HALFWAY. Derived from bucket_pace rather than restated, so
    these tests cannot drift from the arithmetic they are checking."""
    p = bucket_pace(session_bucket(CROSSING_PCT), HALFWAY, M0, M1, BAND)
    brake = START + ((p["pess"] - M0) / SPAN) * SESSION_WINDOW - HALFWAY
    return brake, p["wake"] - HALFWAY


def test_an_over_hold_does_not_end_at_the_brake_line(running):
    """The regression that matters most: a hold begun above the brake line must
    run down to the THROTTLE line, not stop at the brake line on its way past.

    Releasing at the brake line hands the agent back with `pess == allowed` --
    zero headroom -- so the next 1% tick brakes it again. That is exactly the
    sawtooth the band was built to break, and a band that releases there has
    bought nothing while appearing to work: `hook.log` says `line-caught-up`,
    which is what a correct release says too.
    """
    brake_at, throttle_at = _crossings()
    assert throttle_at > brake_at * 5          # the two are far apart

    clk, why = running(entry=paced(band=BAND),
                       buckets={"session": session_bucket(CROSSING_PCT)})

    assert why == "line-caught-up"
    assert clk.elapsed > brake_at * 5, (
        "released at the brake line -- the hysteresis did not happen")
    assert clk.elapsed == pytest.approx(throttle_at, abs=16)

    # And it really is a band under the line, not merely later in time.
    at_release = bucket_pace(session_bucket(CROSSING_PCT), clk.t, M0, M1, BAND)
    assert at_release["region"] == "free"
    assert at_release["allowed"] - at_release["pess"] == pytest.approx(BAND, abs=0.1)


def test_band_delay_does_not_cut_short_a_hold_that_began_over_the_line(running):
    """The same, with a band_delay set -- which is the more dangerous shape.

    Coming down through the band, a naive reading sees region == 'band' and
    applies the band cap. But `brake_start` is the start of the WHOLE hold, so
    `now - brake_start` is already far past any band_delay and the release
    fires instantly. The hold has to stay hard all the way down: band_delay
    governs a lower gear entered from below, never a descent from above.
    """
    _brake_at, throttle_at = _crossings()
    clk, why = running(entry=paced(band=BAND, band_delay=30),
                       buckets={"session": session_bucket(CROSSING_PCT)})

    assert why == "line-caught-up"
    assert clk.elapsed == pytest.approx(throttle_at, abs=16)


def test_the_descent_is_logged_as_a_brake_not_a_throttle(running):
    """One hold, one log line, and it says `brake`.

    Passing through the band on the way down must not re-open as a throttle:
    the reader is told an unmatched `throttle` is ordinary band noise, and this
    hold is the kind that can run to the harness timeout.
    """
    running(entry=paced(band=BAND, band_delay=30),
            buckets={"session": session_bucket(CROSSING_PCT)})
    opens = [m for m in running.logs if " cwd=" in m and not m.startswith("release")]
    assert len(opens) == 1
    assert opens[0].startswith("brake")
    assert not opens[0].startswith("brake*")


def test_a_hold_that_begins_inside_the_band_is_still_only_band_delay(running):
    """The contrast that keeps the fix honest. Entering the band from BELOW is
    a lower gear, not a descent, so it costs one band_delay and no more --
    otherwise the throttle would silently become a full stop and every step in
    the band would cost hours."""
    clk, why = running(entry=paced(band=BAND, band_delay=30),
                       buckets={"session": session_bucket(BAND_PCT)})
    assert why == "band-release"
    assert clk.elapsed == pytest.approx(30, abs=2)


def test_max_delay_still_cuts_a_descent_short(running):
    """max_delay keeps its meaning on the way down too: it is the one knob that
    ends a hold early, and a descent is still a hold. Without this the band
    would quietly make max_delay unenforceable for the longest holds there
    are."""
    clk, why = running(entry=paced(band=BAND, band_delay=30, max_delay=90),
                       buckets={"session": session_bucket(CROSSING_PCT)})
    assert why == "max_delay-release"
    assert clk.elapsed == pytest.approx(90, abs=16)


# --- a hand-edited policy.json must never un-pace a folder -------------------

def test_a_quoted_band_does_not_un_pace_the_folder(cwd):
    """`"band": "2"` is the obvious slip in a hand-edited policy, and it used to
    raise TypeError out of bucket_pace. The hook fails open, so the folder then
    ran COMPLETELY unpaced on every tool call while `status` still called it
    paced -- the one outcome this tool exists to prevent.

    design-decisions section 17 states the rule: an unparseable config must not
    silently un-pace a folder. The value falls back; the pacing survives.
    """
    d = hook.decide(policy_for(cwd, paced(band="2")),
                    state_for({"session": session_bucket(HOT_PCT)}),
                    cwd, HALFWAY)
    assert d["braked"] is True
    assert d["region"] == "over"


@pytest.mark.parametrize("value", ["nonsense", True, [], {}, float("nan"),
                                   float("inf")])
def test_no_band_value_can_stop_the_brake(cwd, value):
    """Whatever is in the file, a bucket far over the line still brakes.

    NaN earns its place here: every comparison against it is False, so a NaN
    band would not crash -- it would quietly read as "never over the line",
    which is worse than a crash because nothing would ever surface it.
    """
    d = hook.decide(policy_for(cwd, paced(band=value, band_delay=5)),
                    state_for({"session": session_bucket(HOT_PCT)}),
                    cwd, HALFWAY)
    assert d["braked"] is True
    assert d["region"] == "over"


def test_a_quoted_band_delay_does_not_un_pace_the_folder(running):
    """The same for band_delay, which used to raise later -- inside the hold,
    after a `throttle` line had already been written, so the log recorded a
    brake that the agent then sailed straight through."""
    with pytest.raises(StillHolding):
        running(entry=paced(band=BAND, band_delay="30"),
                buckets={"session": session_bucket(HOT_PCT)},
                clock=Probe(stop_after=300))


def test_a_zero_band_delay_is_no_throttle_rather_than_an_instant_release(cwd):
    """Read as unset, not as a hold of zero seconds. The difference is in
    hook.log: an instant release would write a throttle/release pair for every
    tool call in the band, for a hold that never happened."""
    d = hook.decide(policy_for(cwd, paced(band=BAND, band_delay=0)),
                    state_for({"session": session_bucket(BAND_PCT)}),
                    cwd, HALFWAY)
    assert d["braked"] is False
    assert d["region"] == "band"      # still reported, so the refresh still fires


def test_a_zero_chunk_cannot_spin_the_loop(running):
    """A chunk of 0 would make the nap zero wherever no cap is in force, and the
    hook would burn a core re-deciding instead of sleeping. Floored at 1s."""
    clk, _why = running(entry=paced(band=BAND, chunk=0, max_delay=10),
                        buckets={"session": session_bucket(HOT_PCT)})
    assert clk.naps, "it never slept at all"
    assert min(clk.naps) >= 1.0
