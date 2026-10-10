"""blind_delay: a bounded wait for a hold that cannot see.

A blind hold is one no data justifies: nothing usable in the snapshot, a stale
snapshot under the line with the refresh failing, or every hot bucket
wake-less (unusable, or over the m0 floor with no reset clause). Before this
it had no ceiling but the harness's 48h timeout, and the rule it now answers
to is the user's: never stall forever, fail free rather than freeze. Past
`blind_delay` (default 240s, under the 5-minute prompt cache) the call goes
through as `blind-release`, and the next one looks again.

What it must NOT touch matters as much: a confident brake -- over the line
with a real wake, stale or not -- and a soft (band) hold. See
harness/blind-delay-plan.md, decisions B1-B6.

The clock is virtual throughout, as in test_max_delay.py and test_band.py. A
test that really slept would take as long as the hold it asserts.
"""

import io
import json

import pytest

from niceclaude import cli, hook
from niceclaude._shared import (DEFAULT_BLIND_DELAY, DEFAULT_POLICY,
                                bucket_pace, norm_path)

SESSION_WINDOW = 5 * 3600
RESETS = 1_760_000_000                      # fixed epoch; never the real clock
START = RESETS - SESSION_WINDOW
HALFWAY = START + SESSION_WINDOW // 2       # f_t = 0.5, so allowed = 48.5

# m0 5, m1 8: at 90% the solved release is ~2.4h out, far past every cap here,
# so a release inside these tests is a cap's doing, never the line's.
HOT_PCT = 90
FREE_PCT = 4                                # under even the m0 floor's pess
BAND_PCT = 45                               # pess 46: between 41.5 and 48.5


def session_bucket(pct):
    return {"pct": pct, "resets_epoch": RESETS,
            "window_seconds": SESSION_WINDOW, "label": None}


def floating_bucket(pct):
    """No reset clause: judged against the m0 floor, with no wake to solve."""
    return {"pct": pct, "resets_epoch": None, "window_seconds": None,
            "label": None}


UNUSABLE = {"pct": None, "resets_epoch": RESETS,
            "window_seconds": SESSION_WINDOW, "label": None}

DEFAULTS = {"m0": 5, "m1": 8, "chunk": 15}


def policy_for(cwd, entry=None, defaults=None):
    rule = {"paced": True, "model": "opus"}
    rule.update(entry or {})
    return {"global": {"enabled": True},
            "defaults": dict(DEFAULTS) if defaults is None else defaults,
            "paths": {cwd: rule}}


def state_for(buckets, age=10):
    return {"ts_epoch": HALFWAY - age, "buckets": buckets}


@pytest.fixture
def cwd(tmp_path):
    d = tmp_path / "work"
    d.mkdir()
    return norm_path(str(d))


class StillHolding(Exception):
    """The clock reached its probe point with run() still asleep."""


class Clock:
    """Advances only when the code under test sleeps (or a fake refresh says
    it took time). `stop_after` turns "it never releases" into something a
    test can assert: still being asleep at that point is the pass."""

    def __init__(self, stop_after=None, max_naps=2000):
        self.t = float(HALFWAY)
        self.naps = []
        self.stop_after = stop_after
        self.max_naps = max_naps

    def time(self):
        return self.t

    def sleep(self, seconds):
        if self.stop_after is not None and self.elapsed >= self.stop_after:
            raise StillHolding
        self.naps.append(seconds)
        if len(self.naps) > self.max_naps:
            raise AssertionError("run() did not release; it is still holding")
        self.t += seconds

    @property
    def elapsed(self):
        return self.t - HALFWAY


@pytest.fixture
def held(cwd, tmp_path, monkeypatch):
    """A full `hook.run` with the policy, snapshot, refresh and clock faked.

    `buckets` is a dict, or a callable of elapsed seconds giving one, for a
    snapshot that changes under a hold. `stale` makes every snapshot too old
    to trust; `refresh` replaces the refresh (default: always fails). Returns
    (clock, reason); messages the hook logged are on `.logs`.
    """
    policy_path = tmp_path / "policy.json"
    monkeypatch.setattr(hook, "POLICY_PATH", str(policy_path))
    messages = []
    monkeypatch.setattr(hook, "log", messages.append)

    def go(entry=None, buckets=None, defaults=None, clock=None, stale=False,
           refresh=None, event=None):
        policy_path.write_text(json.dumps(policy_for(cwd, entry, defaults)),
                               encoding="utf-8")
        clk = clock or Clock()
        at = buckets if callable(buckets) else (lambda _e: buckets or {})
        age = 9999 if stale else 10
        monkeypatch.setattr(
            hook, "load_state",
            lambda path, key: (state_for(at(clk.elapsed), age), None))
        monkeypatch.setattr(hook, "snapshot_age", lambda state, now: age)
        monkeypatch.setattr(hook, "refresh_snapshot",
                            refresh or (lambda: False))
        monkeypatch.setattr(hook.time, "time", clk.time)
        monkeypatch.setattr(hook.time, "sleep", clk.sleep)
        _brake_start, reason = hook.run(cwd, event)
        return clk, reason

    go.logs = messages
    return go


# The three kinds of blind hold (B2), as (buckets, stale) for `held`.
BLIND_KINDS = {
    "no usable buckets": ({}, False),
    "stale and refresh failing": ({"session": session_bucket(FREE_PCT)}, True),
    "hot, over the floor": ({"session": floating_bucket(40)}, False),
    "hot, unusable": ({"session": UNUSABLE}, False),
}


# --- B1: the setting, and where it comes from --------------------------------

def test_the_shipped_default_is_four_minutes():
    """Under the 5-minute prompt cache TTL, which is the whole reason for the
    number: a blind hold that outlived it would make every blind call re-read
    its context from cold."""
    assert DEFAULT_BLIND_DELAY == 240
    assert DEFAULT_POLICY["defaults"]["blind_delay"] == 240


def test_240_applies_with_no_key_anywhere(cwd):
    """A policy.json written before the knob existed gets the default. That
    is the behaviour change, and it is intended."""
    d = hook.decide(policy_for(cwd), state_for({}), cwd, HALFWAY)
    assert d["blind"] is True
    assert d["blind_delay"] == 240


def test_defaults_supply_it_and_the_rule_overrides_it(cwd):
    defaults = dict(DEFAULTS, blind_delay=600)
    inherited = hook.decide(policy_for(cwd, defaults=defaults),
                            state_for({}), cwd, HALFWAY)
    assert inherited["blind_delay"] == 600
    ruled = hook.decide(policy_for(cwd, {"blind_delay": 30}, defaults),
                        state_for({}), cwd, HALFWAY)
    assert ruled["blind_delay"] == 30


def test_an_explicit_null_is_off_even_over_a_default(cwd):
    """The key's presence picks the source (off_or_num): a written null must
    not fall back to the defaults block or the built-in and turn back on."""
    defaults = dict(DEFAULTS, blind_delay=600)
    d = hook.decide(policy_for(cwd, {"blind_delay": None}, defaults),
                    state_for({}), cwd, HALFWAY)
    assert d["blind_delay"] is None


@pytest.mark.parametrize("raw, want", [(-5, 0), (0, 0), ("90", 90),
                                       ("soon", 240), ("inf", 240)])
def test_odd_values_clamp_or_fall_back(cwd, raw, want):
    """Negative means "never hold blind", like 0. Garbage is the default, as
    everywhere else in the policy -- not off, which would be the unbounded
    hold this knob exists to end."""
    d = hook.decide(policy_for(cwd, {"blind_delay": raw}), state_for({}),
                    cwd, HALFWAY)
    assert d["blind_delay"] == want


def test_it_is_surfaced_on_every_verdict(cwd):
    """`run` reads it from whatever decide returned, and a hold can turn blind
    mid-way, so it cannot be only on the blind verdicts."""
    for buckets in ({"session": session_bucket(FREE_PCT)},
                    {"session": session_bucket(BAND_PCT)},
                    {"session": session_bucket(HOT_PCT)}):
        d = hook.decide(policy_for(cwd), state_for(buckets), cwd, HALFWAY)
        assert d["blind_delay"] == 240


# --- B2: what counts as blind ------------------------------------------------

def test_a_hot_hold_with_no_real_wake_is_blind(cwd):
    """B2.3, the new kind. Over the m0 floor with no reset clause, the hold
    re-checks on the chunk for a wake it cannot compute: "cannot tell", the
    same footing as an empty snapshot."""
    for b in (floating_bucket(40), UNUSABLE):
        d = hook.decide(policy_for(cwd), state_for({"session": b}), cwd,
                        HALFWAY)
        assert d["braked"] is True and d["hold"] == "hard"
        assert d["blind"] is True


def test_one_real_wake_makes_a_hot_hold_a_real_brake(cwd):
    """Mixed hot: a wake-less bucket beside one over the line with a reset.
    The real one justifies the hold, so it is not blind."""
    buckets = {"session": session_bucket(HOT_PCT),
               "week:all models": floating_bucket(40)}
    d = hook.decide(policy_for(cwd), state_for(buckets), cwd, HALFWAY)
    assert d["braked"] is True
    assert d["blind"] is False


def test_stale_but_over_with_a_real_wake_stays_confident(cwd):
    """Design-decisions §12: usage only rises, so a stale reading already over
    the line is a floor. That is a brake data justifies, not blindness."""
    d = hook.decide(policy_for(cwd), state_for({"session": session_bucket(
        HOT_PCT)}, age=9999), cwd, HALFWAY, degraded=True)
    assert d["braked"] is True
    assert d["blind"] is False


def test_a_soft_hold_is_never_blind(cwd):
    d = hook.decide(policy_for(cwd), state_for({"session": session_bucket(
        BAND_PCT)}), cwd, HALFWAY)
    assert d["hold"] == "soft"
    assert d["blind"] is False


# --- the release, in a full run ----------------------------------------------

@pytest.mark.parametrize("kind", sorted(BLIND_KINDS))
def test_every_kind_of_blind_hold_ends_at_blind_delay(held, kind):
    """The point of the knob: no blind hold outlives it, whichever kind."""
    buckets, stale = BLIND_KINDS[kind]
    clk, reason = held(buckets=buckets, stale=stale)
    assert reason == "blind-release"
    assert clk.elapsed == 240


@pytest.mark.parametrize("kind", sorted(BLIND_KINDS))
def test_an_explicit_null_holds_past_it(held, kind):
    """`--no-blind-delay` is the old hold-until-seen behaviour, kept opt-in."""
    buckets, stale = BLIND_KINDS[kind]
    with pytest.raises(StillHolding):
        held(entry={"blind_delay": None}, buckets=buckets, stale=stale,
             clock=Clock(stop_after=3600))


def test_zero_never_holds_blind(held):
    """Released on the first blind pass, without a single nap."""
    clk, reason = held(entry={"blind_delay": 0}, buckets={})
    assert reason == "blind-release"
    assert clk.naps == []


def test_the_nap_is_bounded_by_the_blind_deadline(held):
    """As it is by `cap`: a blind_delay shorter than the chunk must not sleep
    a whole chunk past it."""
    clk, reason = held(entry={"blind_delay": 20}, buckets={})
    assert reason == "blind-release"
    assert clk.naps == [15, 5]
    assert clk.elapsed == 20


def test_a_confident_stale_brake_is_not_released_by_it(held):
    """Stale but already over the line with a real wake: it can only have got
    worse, so this is a brake data justifies, and blind_delay is not its
    business. Only max_delay may cut it short (design-decisions §12)."""
    with pytest.raises(StillHolding):
        held(buckets={"session": session_bucket(HOT_PCT)}, stale=True,
             clock=Clock(stop_after=1200))


def test_a_mixed_hot_hold_is_not_released_by_it(held):
    buckets = {"session": session_bucket(HOT_PCT),
               "week:all models": floating_bucket(40)}
    with pytest.raises(StillHolding):
        held(buckets=buckets, clock=Clock(stop_after=1200))


def test_a_soft_hold_keeps_its_own_cap(held):
    """A band hold is in sight of the line, so blind_delay has no say in it,
    even when it is the shorter of the two."""
    clk, reason = held(entry={"band_delay": 600, "blind_delay": 10},
                       buckets={"session": session_bucket(BAND_PCT)})
    assert reason == "band-release"
    assert clk.elapsed == 600


# --- B3: the clock -----------------------------------------------------------

def test_refresh_time_before_the_hold_counts(held):
    """Blind from the first pass, the clock starts at the start of `run`: a
    slow failing refresh was time spent not seeing, and leaving it out would
    let it stretch every blind hold."""
    clk = Clock()
    calls = []

    def slow_failing_refresh():
        if not calls:
            clk.t += 100
        calls.append(1)
        return False

    clk, reason = held(buckets={"session": session_bucket(FREE_PCT)},
                       stale=True, refresh=slow_failing_refresh, clock=clk)
    assert reason == "blind-release"
    assert clk.elapsed == 240


def test_the_clock_resets_when_the_hold_stops_being_blind(held):
    """Blind, then a real brake, then blind again. What is bounded is
    continuous blindness: the real stretch is not blindness and must not
    spend the next blind stretch's allowance -- nor leave the first one's
    spent. Without the reset this releases at 240."""
    def at(elapsed):
        if 200 <= elapsed < 400:
            return {"session": session_bucket(HOT_PCT)}
        return {}

    clk, reason = held(buckets=at)
    assert reason == "blind-release"
    # Blind again from the first pass at or after 400 (405, on 15s naps).
    assert clk.elapsed == 405 + 240


def test_a_hold_that_turns_blind_midway_counts_from_then(held):
    """Real brake first, then the snapshot goes empty: the brake's time is not
    blind time, so the release is a full blind_delay after the turn."""
    def at(elapsed):
        return {"session": session_bucket(HOT_PCT)} if elapsed < 300 else {}

    clk, reason = held(buckets=at)
    assert reason == "blind-release"
    assert clk.elapsed == 300 + 240


# --- B4: caps combine --------------------------------------------------------

def test_max_delay_first_keeps_its_own_label(held):
    clk, reason = held(entry={"max_delay": 60}, buckets={})
    assert reason == "max_delay-release-WHILE-BLIND"
    assert clk.elapsed == 60


def test_blind_delay_first_is_a_blind_release(held):
    clk, reason = held(entry={"max_delay": 600}, buckets={})
    assert reason == "blind-release"
    assert clk.elapsed == 240


def test_a_tie_is_called_blind(held):
    """Both fall on the same instant: "could not see" is the truer reason."""
    _clk, reason = held(entry={"max_delay": 240}, buckets={})
    assert reason == "blind-release"


def test_max_delay_alone_still_releases_a_confident_brake(held):
    """And blind_delay does not borrow its label for one that is not blind."""
    clk, reason = held(entry={"max_delay": 300, "blind_delay": 10},
                       buckets={"session": session_bucket(HOT_PCT)})
    assert reason == "max_delay-release"
    assert clk.elapsed == 300


# --- B5: the next call -------------------------------------------------------

def test_the_next_call_holds_again(held):
    """No memory across invocations: each tool call looks afresh and, still
    blind, holds up to blind_delay again. One step per four minutes, warm."""
    clk = Clock()
    held(buckets={}, clock=clk)
    first = clk.elapsed
    _clk, reason = held(buckets={}, clock=clk)
    assert reason == "blind-release"
    assert clk.elapsed - first == 240


def test_main_logs_the_release_by_name(held, cwd, monkeypatch):
    """hook.log is the only overnight record, and "blind-release" is what a
    reader greps for to count calls that went through on no data."""
    held(entry={"blind_delay": 0}, buckets={})   # writes the policy file
    clk = Clock()
    monkeypatch.setattr(hook.time, "time", clk.time)
    monkeypatch.setattr(hook, "run", lambda c, e, p: (clk.t - 240,
                                                      "blind-release"))
    monkeypatch.setattr(hook.sys, "stdin",
                        io.StringIO(json.dumps({"cwd": cwd})))
    assert hook.main() == 0
    assert held.logs[-1] == f"release cwd={cwd} after 240s (blind-release)"


# --- B6: the CLI -------------------------------------------------------------

@pytest.fixture
def policy(tmp_path, monkeypatch):
    """A redirected policy file and a folder to write rules for."""
    path = tmp_path / "policy.json"
    monkeypatch.setattr(cli, "POLICY_PATH", str(path))
    target = tmp_path / "proj"
    target.mkdir()

    def entry():
        return json.loads(path.read_text(encoding="utf-8"))["paths"][
            norm_path(str(target))]

    return target, entry, path


def test_on_round_trips_both_flags(policy, capsys):
    """Through `main`, so the argparse dest names are covered too."""
    target, entry, _path = policy
    assert cli.main(["on", str(target), "--blind-delay", "600"]) == 0
    assert entry()["blind_delay"] == 600
    assert cli.main(["on", str(target), "--no-blind-delay"]) == 0
    assert "blind_delay" in entry() and entry()["blind_delay"] is None
    assert cli.main(["on", str(target), "--blind-delay", "0"]) == 0
    assert entry()["blind_delay"] == 0
    capsys.readouterr()


def test_on_without_the_flags_leaves_it_alone(policy, capsys):
    """`on` changes only what it is told to, so a rule's blind_delay survives
    an unrelated edit -- and a rule without one keeps inheriting."""
    target, entry, path = policy
    cli.main(["on", str(target), "--blind-delay", "90"])
    cli.main(["on", str(target), "--m0", "3"])
    assert entry()["blind_delay"] == 90
    other = target / "sub"
    other.mkdir()
    cli.main(["on", str(other)])
    capsys.readouterr()
    pol = json.loads(path.read_text(encoding="utf-8"))
    assert "blind_delay" not in pol["paths"][norm_path(str(other))]


def test_no_blind_delay_beats_a_defaults_level_value(policy, capsys):
    """A pop would inherit the default, so "hold until you can see" has to be
    an explicit null -- the same trap --no-max-delay is written around."""
    target, _entry, path = policy
    path.write_text(json.dumps({"global": {"enabled": True},
                                "defaults": dict(DEFAULTS, blind_delay=600),
                                "paths": {}}), encoding="utf-8")
    cli.main(["on", str(target), "--model", "opus", "--no-blind-delay"])
    capsys.readouterr()
    pol = json.loads(path.read_text(encoding="utf-8"))
    cwd = norm_path(str(target))
    assert hook.decide(pol, state_for({}), cwd, HALFWAY)["blind_delay"] is None


def test_setting_and_clearing_at_once_is_refused(policy, capsys):
    target, _entry, _path = policy
    with pytest.raises(SystemExit) as exc:
        cli.main(["on", str(target), "--blind-delay", "30",
                  "--no-blind-delay"])
    assert exc.value.code == 2
    assert "not allowed with" in capsys.readouterr().err


def test_on_help_mentions_it(capsys):
    with pytest.raises(SystemExit):
        cli.main(["on", "--help"])
    out = capsys.readouterr().out
    assert "--blind-delay" in out and "--no-blind-delay" in out
    assert "blind-release" in out


@pytest.fixture
def status(tmp_path, monkeypatch):
    """`cmd_status` on a paced folder, a snapshot, and a frozen clock.

    Returns a callable taking rule settings, buckets and the snapshot's
    timestamp, and giving back what status printed."""
    monkeypatch.setattr(cli, "POLICY_PATH", str(tmp_path / "policy.json"))
    monkeypatch.setattr(cli, "STATE_PATH", str(tmp_path / "state.json"))
    monkeypatch.setattr(cli.time, "time", lambda: float(HALFWAY))
    work = tmp_path / "work"
    work.mkdir()
    cwd = norm_path(str(work))

    def go(capsys, buckets, ts_epoch=HALFWAY - 10, **rule):
        entry = {"paced": True, "model": "opus", "enforce": "session"}
        entry.update(rule)
        cli.save_policy({"global": {"enabled": True},
                         "defaults": dict(DEFAULTS), "paths": {cwd: entry}})
        cli.write_atomic(cli.STATE_PATH, json.dumps(
            {"ts_epoch": ts_epoch, "ok": True, "buckets": buckets}))
        capsys.readouterr()
        cli.cmd_status(cwd)
        return capsys.readouterr().out

    return go


def test_status_prints_the_line_beside_band_delay(status, capsys):
    out = status(capsys, {"session": session_bucket(FREE_PCT)})
    lines = out.splitlines()
    i = next(n for n, line in enumerate(lines)
             if line.startswith("  band_delay"))
    assert lines[i + 1] == ("  blind_delay   4m00s, then a blind hold lets "
                            "the call through")


def test_status_says_when_it_is_off(status, capsys):
    out = status(capsys, {"session": session_bucket(FREE_PCT)},
                 blind_delay=None)
    assert "  blind_delay   none -- a blind hold waits until it can see" in out
    out = status(capsys, {"session": session_bucket(FREE_PCT)}, blind_delay=0)
    assert "  blind_delay   0 -- never holds blind" in out


def test_the_blind_verdict_says_when_the_call_goes_through(status, capsys):
    out = status(capsys, {"session": session_bucket(FREE_PCT)},
                 ts_epoch=HALFWAY - 4000)
    assert "BRAKED, blind" in out
    assert "blind_delay lets the call through after 4m00s" in out
    out = status(capsys, {"session": session_bucket(FREE_PCT)},
                 ts_epoch=HALFWAY - 4000, blind_delay=None)
    assert "blind_delay is off" in out
    out = status(capsys, {"session": session_bucket(FREE_PCT)},
                 ts_epoch=HALFWAY - 4000, max_delay=60)
    assert "max_delay lets the call through after 1m00s" in out


def test_a_wake_less_hot_hold_reports_blind(status, capsys):
    """B2.3 reaches `status` through the hook's own verdict, and the row says
    the hold is bounded rather than leaving "unsolvable" to read as forever."""
    out = status(capsys, {"session": floating_bucket(40)})
    assert "BRAKED, blind -- session 40% over floor 5%" in out
    row = next(r for r in out.splitlines() if r.startswith("  session"))
    assert "unsolvable, re-checks every 15s, through after 4m00s" in row
    out = status(capsys, {"session": floating_bucket(40)}, blind_delay=None)
    row = next(r for r in out.splitlines() if r.startswith("  session"))
    assert row.endswith("unsolvable, re-checks every 15s, no blind_delay")


def test_an_unsolvable_row_beside_a_real_wake_does_not_claim_release(
        status, capsys):
    """Mixed hot is a real brake: the row with a wake says when it ends, and
    the unsolvable one must not promise a blind release that will not come."""
    out = status(capsys, {"session": session_bucket(HOT_PCT),
                          "week:all models": floating_bucket(40)},
                 enforce=["session", "week"])
    assert "BRAKED, blind" not in out
    row = next(r for r in out.splitlines() if r.startswith("  week:all"))
    assert row.endswith("unsolvable, re-checks every 15s")


# --- review round 1 ----------------------------------------------------------

def past_reset_bucket(pct, resets=HALFWAY - 60):
    """Over the line on a reading taken before a reset that has since passed:
    bucket_pace clamps its wake to that reset, so the wake is in the past."""
    return {"pct": pct, "resets_epoch": resets,
            "window_seconds": SESSION_WINDOW, "label": None}


def test_a_wake_already_past_is_not_a_real_one(cwd):
    """A window that has reset since the reading voids the lower bound: usage
    was zeroed, so "it can only have got worse" no longer holds. Stale, with
    the refresh failing, that is a hold on nothing -- blind, at every point
    after the reset, not a confident brake for as long as the outage lasts."""
    week = 7 * 86400
    for after in (60, 3600, 4 * 3600):
        state = {"ts_epoch": HALFWAY - week // 2,
                 "buckets": {"week:all models": {
                     "pct": 94, "resets_epoch": HALFWAY - after,
                     "window_seconds": week, "label": None}}}
        d = hook.decide(policy_for(cwd), state, cwd, HALFWAY, degraded=True,
                        hard=True)
        assert d["braked"] is True
        assert d["blind"] is True, after


def test_a_stale_brake_past_its_reset_ends_at_blind_delay(held):
    clk, reason = held(buckets={"session": past_reset_bucket(95)}, stale=True)
    assert reason == "blind-release"
    assert clk.elapsed == 240
    # On the chunk, not the 1s floor a past wake used as wake_at would give.
    assert clk.naps == [15] * 16


def test_no_refresh_is_started_once_the_allowance_is_spent(held):
    """The last nap lands exactly on the deadline. A refresh started there is
    synchronous -- up to 180s in life, 100s here -- and would run before the
    release check, so the call would cost blind_delay plus a refresh.

    The refresh "succeeds" without making the snapshot fresh, so the backoff
    never engages and one is due on every pass, the deadline's included."""
    clk = Clock()

    def refresh():
        if clk.elapsed >= 240:
            clk.t += 100
            return False
        return True

    clk, reason = held(buckets={"session": session_bucket(FREE_PCT)},
                       stale=True, refresh=refresh, clock=clk)
    assert reason == "blind-release"
    assert clk.elapsed == 240


def test_a_per_call_row_is_bounded_for_its_own_caller(status, capsys):
    """Under `detect` the verdict is asked with no caller, so it cannot see a
    `per call` row. A Fable caller held only by a wake-less week:Fable is
    blind, and the hook lets it through at blind_delay; the row has to say
    so rather than read as an unbounded hold."""
    out = status(capsys, {"session": session_bucket(FREE_PCT),
                          "week:all models": session_bucket(FREE_PCT),
                          "week:Fable": floating_bucket(40)},
                 model="detect", enforce=["session", "week", "model"])
    row = next(r for r in out.splitlines() if r.startswith("  week:Fable"))
    assert "per call" in row
    assert row.endswith("unsolvable, re-checks every 15s, through after 4m00s")


# --- review round 2 ----------------------------------------------------------

WEEK = 7 * 86400
# Mid-window like the session bucket, so its lines sit at 48.5 / 41.5 too and
# 45% is in the band -- with a band wake ~9h out, on the weekly line's slope.
WEEK_IN_BAND = {"pct": BAND_PCT, "resets_epoch": HALFWAY + WEEK // 2,
                "window_seconds": WEEK, "label": None}


def over_then_unusable(elapsed):
    """Over the line for 30s, then the session reading turns unusable while
    the week sits in the band: a hold latched hard, running down."""
    if elapsed < 30:
        return {"session": session_bucket(HOT_PCT),
                "week:all models": WEEK_IN_BAND}
    return {"session": UNUSABLE, "week:all models": WEEK_IN_BAND}


def test_a_running_down_hold_is_not_blinded_by_an_unusable_sibling(held):
    """The band wake is the release the hard hold is aiming at, and it is
    real. An unusable session beside it must not turn that into blindness."""
    with pytest.raises(StillHolding):
        held(entry={"enforce": ["session", "week"]},
             buckets=over_then_unusable, clock=Clock(stop_after=1200))


def test_a_stale_band_still_justifies_a_latched_descent(held):
    """Stale, the band reading is still a lower bound: a pct above the
    throttle line proves the true one is above it too. So the descent the
    hold latched on at 0s is still owed, and the unusable session beside it
    does not make it blind. (Round 2 had this releasing at 270s, applying
    the "stale data never proceeds" rule to a hold, which is not proceeding.)
    """
    with pytest.raises(StillHolding):
        held(entry={"enforce": ["session", "week"]},
             buckets=over_then_unusable, stale=True,
             clock=Clock(stop_after=1200))


def test_status_reads_a_past_reset_row_as_unsolvable(status, capsys):
    """Not "HOLDS 0s": the hook re-checks such a row on the chunk, blind."""
    out = status(capsys, {"session": past_reset_bucket(95)},
                 ts_epoch=HALFWAY - 4000)
    row = next(r for r in out.splitlines() if r.startswith("  session"))
    assert row.endswith("unsolvable, re-checks every 15s, through after 4m00s")


def test_status_survives_a_quoted_max_delay(status, capsys):
    """The hook coerces "300"; status used to crash on min() with it."""
    out = status(capsys, {"session": session_bucket(FREE_PCT)},
                 ts_epoch=HALFWAY - 4000, max_delay="300")
    assert "  max_delay     5m00s" in out
    assert "blind_delay lets the call through after 4m00s" in out


def test_status_clamps_a_negative_max_delay(status, capsys):
    """The hook clamps it to 0; status must not print a release in the past."""
    out = status(capsys, {"session": session_bucket(FREE_PCT)},
                 ts_epoch=HALFWAY - 4000, max_delay=-60)
    assert "  max_delay     0s" in out
    assert "max_delay lets the call through after 0s" in out
    assert "-1m" not in out


# --- review round 3 ----------------------------------------------------------

def test_a_blind_hold_does_not_latch_hard(held):
    """It never saw the brake line, so a band bucket beside the unusable one
    has no descent to justify. Latched, the next pass un-blinded it and held
    to the band wake: 1185s here, hours on a real weekly line."""
    clk, reason = held(buckets={"session": UNUSABLE,
                                "week:all models": WEEK_IN_BAND})
    assert reason == "blind-release"
    assert clk.elapsed == 240


def test_a_blind_hold_that_sees_the_band_becomes_a_throttle(held):
    """Blind first, then a fresh in-band reading: with nothing latched that is
    an ordinary soft hold at band_delay (default 180s, counted from the start
    of the hold), not a multi-hour "[running down]" hold."""
    def at(elapsed):
        if elapsed < 60:
            return {"session": UNUSABLE, "week:all models": WEEK_IN_BAND}
        return {"session": session_bucket(BAND_PCT),
                "week:all models": WEEK_IN_BAND}

    clk, reason = held(buckets=at)
    assert reason == "band-release"
    assert clk.elapsed == 180
    assert not any("running down" in m for m in held.logs)


def test_no_blind_delay_help_names_max_delay(capsys):
    """Off, blind_delay leaves max_delay as a ceiling; the help must not say
    the harness timeout is the only one."""
    with pytest.raises(SystemExit):
        cli.main(["on", "--help"])
    out = " ".join(capsys.readouterr().out.split())
    assert "only ceiling" not in out
    assert "only max_delay, if set, or the harness timeout then ends it" in out


# --- review round 4: one rule ------------------------------------------------
#
# A hard hold is blind iff no bucket governing it has a wake strictly in the
# future that its reading supports. Governing: the hot entries, plus the band
# entries once `hard` is latched. Staleness does not enter into it.

DESCENT_PCT = 47      # pess 48: in the band, throttle line 41.5 at HALFWAY


def hot_then_band(elapsed):
    """Over the line for 30s, then the rising line moves the (frozen) reading
    into the band: a descent latched on real data, with refresh lost."""
    pct = HOT_PCT if elapsed < 30 else DESCENT_PCT
    return {"session": session_bucket(pct)}


def test_a_stale_descent_is_held_not_blind_released(held):
    with pytest.raises(StillHolding):
        held(buckets=hot_then_band, stale=True, clock=Clock(stop_after=1200))


def test_a_stale_descent_ends_no_earlier_than_its_band_wake(held):
    """At the band wake the throttle line meets pess, the stale reading is
    then merely "under the line", and that is the old degraded blind hold --
    which blind_delay releases. What must not happen is a release before it."""
    wake = bucket_pace(session_bucket(DESCENT_PCT), HALFWAY, 5, 8, 7)["wake"]
    clk, reason = held(buckets=hot_then_band, stale=True)
    assert reason == "blind-release"
    assert clk.t >= wake + 240


def test_a_latched_band_whose_window_has_reset_is_blind(held):
    """Degraded, hard, band bucket past its reset: its wake is in the past, so
    it supports nothing. Blind from the turn at 30s; released 240s later."""
    past_band = {"pct": 87, "resets_epoch": HALFWAY - 60,
                 "window_seconds": WEEK, "label": None}

    def at(elapsed):
        if elapsed < 30:
            return {"session": session_bucket(HOT_PCT)}
        return {"session": session_bucket(FREE_PCT),
                "week:all models": past_band}

    clk, reason = held(buckets=at, stale=True)
    assert reason == "blind-release"
    assert clk.elapsed == 30 + 240


HOT_KINDS = {
    "real": (session_bucket(HOT_PCT), True),
    "wake-less": (floating_bucket(40), False),
    "past-wake": (past_reset_bucket(95), False),
}
BAND_KINDS = {
    "future": (WEEK_IN_BAND, True),
    "past": ({"pct": 87, "resets_epoch": HALFWAY - 60,
              "window_seconds": WEEK, "label": None}, False),
    "none": (None, False),
}


@pytest.mark.parametrize("degraded", [False, True])
@pytest.mark.parametrize("hard", [False, True])
@pytest.mark.parametrize("band_kind", sorted(BAND_KINDS))
@pytest.mark.parametrize("hot_kind", sorted(HOT_KINDS))
def test_the_blind_rule(cwd, hot_kind, band_kind, hard, degraded):
    """The whole matrix, against the rule as stated -- the test that would
    have caught review rounds 1 to 4, each of which was one cell of it."""
    hot_bucket, hot_supported = HOT_KINDS[hot_kind]
    band_bucket, band_supported = BAND_KINDS[band_kind]
    buckets = {"session": hot_bucket}
    if band_bucket is not None:
        buckets["week:all models"] = band_bucket
    d = hook.decide(policy_for(cwd), state_for(buckets), cwd, HALFWAY,
                    degraded=degraded, hard=hard)
    assert d["braked"] is True and d["hold"] == "hard"
    assert d["blind"] is not (hot_supported or (hard and band_supported))


# --- review round 5 ----------------------------------------------------------

def test_a_mid_hold_turn_counts_its_own_failed_refresh(held):
    """Turned blind mid-hold, the pass that first sees it refreshes, and that
    refresh is blind time. Counted from after it, a second refresh could still
    be running at the deadline: blind_delay plus two refreshes, where the
    contract allows blind_delay plus at most one in flight."""
    clk = Clock()
    first_blind = []

    def at(elapsed):
        if elapsed < 180:
            return {"session": session_bucket(HOT_PCT)}
        if not first_blind:
            first_blind.append(elapsed)
        return {}

    def slow_failing_refresh():
        clk.t += 170
        return False

    clk, reason = held(buckets=at, stale=True, refresh=slow_failing_refresh,
                       clock=clk)
    assert reason == "blind-release"
    assert clk.elapsed - first_blind[0] <= 240 + 170


def test_zero_mid_hold_turn_starts_no_refresh(held):
    """blind_delay 0, a real brake that turns blind at a pass boundary: the
    allowance is spent the moment it turns, so that pass must not start a
    refresh -- up to 180s in life -- before releasing."""
    clk = Clock()
    refreshes = []

    def at(elapsed):
        return {"session": session_bucket(HOT_PCT)} if elapsed < 30 else {}

    def refresh():            # "succeeds" but stays stale: no backoff
        refreshes.append(clk.elapsed)
        clk.t += 5
        return True

    clk, reason = held(entry={"blind_delay": 0}, buckets=at, stale=True,
                       refresh=refresh, clock=clk)
    assert reason == "blind-release"
    assert all(r < 30 for r in refreshes), refreshes
