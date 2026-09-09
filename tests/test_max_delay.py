"""max_delay: capping one hold, rather than the total restraint.

Without it, a folder over the line holds until the line catches up -- which can
be hours, far longer than the prompt cache survives. The next turn then re-reads
the whole context from cold, so a wait taken to save budget can cost more than
it saved. max_delay caps a single hold; the agent takes one step and the next
PreToolUse brakes again, so the restraint is applied as many short holds.

The clock is virtual throughout. A test that actually slept would take as long
as the behaviour it is asserting.
"""

import json

import pytest

from niceclaude import cli, hook
from niceclaude._shared import norm_path

SESSION_WINDOW = 5 * 3600
RESETS = 1_760_000_000                      # fixed epoch; never the real clock
START = RESETS - SESSION_WINDOW
HALFWAY = START + SESSION_WINDOW / 2        # f_t = 0.5

# m0=5, m1=8 -> span 87. At 90% used the solved release is ~2.4h out, which is
# comfortably longer than every max_delay exercised here -- so any release
# inside these tests is max_delay's doing, not the line catching up.
HOT_PCT = 90


def session_bucket(pct):
    return {"pct": pct, "resets_epoch": RESETS,
            "window_seconds": SESSION_WINDOW, "label": None}


def policy_for(cwd, entry=None, defaults=None):
    return {
        "global": {"enabled": True},
        "defaults": defaults if defaults is not None else {"m0": 5, "m1": 8,
                                                           "chunk": 15},
        "paths": {cwd: entry if entry is not None else {"paced": True,
                                                        "model": "opus"}},
    }


def state_for(buckets, now=HALFWAY, age=10):
    return {"ts_epoch": now - age, "buckets": buckets}


@pytest.fixture
def cwd(tmp_path):
    d = tmp_path / "work"
    d.mkdir()
    return norm_path(str(d))


class Clock:
    """A virtual clock that advances only when the code under test sleeps.

    Sleeps are recorded so a test can assert how the wait was divided, not just
    how long it lasted -- the chunking is the part that lets a policy edit reach
    a frozen agent, so it matters that it survives the max_delay clamp.
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


@pytest.fixture
def braking(cwd, tmp_path, monkeypatch):
    """A paced folder that is over the line, with the clock under our control.

    Returns a callable taking the folder's policy entry and giving back
    (clock, reason) from a full `hook.run`.
    """
    policy_path = tmp_path / "policy.json"
    state_path = tmp_path / "state.json"
    state_path.write_text(json.dumps(state_for({"session": session_bucket(HOT_PCT)})),
                          encoding="utf-8")
    monkeypatch.setattr(hook, "POLICY_PATH", str(policy_path))
    monkeypatch.setattr(hook, "STATE_PATH", str(state_path))
    # The snapshot ages as the virtual clock advances. Left alone it would cross
    # MAX_STALE mid-test and pull the refresh path in, which is a different
    # behaviour under test elsewhere. Keep it fresh so these tests see only the
    # brake loop.
    monkeypatch.setattr(hook, "snapshot_age", lambda state, now: 10)
    monkeypatch.setattr(hook, "log", lambda msg: None)

    def go(entry=None, defaults=None, clock=None):
        policy_path.write_text(json.dumps(policy_for(cwd, entry, defaults)),
                               encoding="utf-8")
        clk = clock or Clock()
        monkeypatch.setattr(hook.time, "time", clk.time)
        monkeypatch.setattr(hook.time, "sleep", clk.sleep)
        _brake_start, reason = hook.run(cwd)
        return clk, reason

    return go


# --- decide surfaces the setting ---------------------------------------------

def test_absent_max_delay_is_none(cwd):
    """Off by default: an unconfigured folder holds until the line catches up."""
    d = hook.decide(policy_for(cwd), state_for({"session": session_bucket(HOT_PCT)}),
                    cwd, HALFWAY)
    assert d["braked"] is True
    assert d["max_delay"] is None


def test_entry_max_delay_is_surfaced(cwd):
    d = hook.decide(policy_for(cwd, entry={"paced": True, "max_delay": 300}),
                    state_for({"session": session_bucket(HOT_PCT)}), cwd, HALFWAY)
    assert d["max_delay"] == 300


def test_defaults_supply_max_delay_and_the_entry_overrides_it(cwd):
    defaults = {"m0": 5, "m1": 8, "chunk": 15, "max_delay": 300}
    inherited = hook.decide(policy_for(cwd, defaults=defaults),
                            state_for({"session": session_bucket(HOT_PCT)}),
                            cwd, HALFWAY)
    assert inherited["max_delay"] == 300

    overridden = hook.decide(
        policy_for(cwd, entry={"paced": True, "max_delay": 60}, defaults=defaults),
        state_for({"session": session_bucket(HOT_PCT)}), cwd, HALFWAY)
    assert overridden["max_delay"] == 60


def test_max_delay_is_surfaced_on_the_blind_path_too(cwd):
    """A blind brake is still a hold, so the cap has to reach it."""
    d = hook.decide(policy_for(cwd, entry={"paced": True, "max_delay": 60}),
                    state_for({}), cwd, HALFWAY, degraded=True)
    assert d["braked"] is True
    assert d["blind"] is True
    assert d["max_delay"] == 60


# --- run honours it ----------------------------------------------------------

def test_run_releases_after_max_delay_while_still_over_the_line(braking):
    clock, reason = braking(entry={"paced": True, "max_delay": 60})
    assert reason == "max_delay-release"
    assert clock.elapsed == pytest.approx(60, abs=1)


def test_the_release_is_not_the_line_catching_up(braking):
    """The distinction the log has to preserve: proceeded-anyway vs cleared."""
    _clock, reason = braking(entry={"paced": True, "max_delay": 60})
    assert reason != "line-caught-up"


def test_without_max_delay_it_keeps_holding(braking):
    """The contrast case -- otherwise the test above proves nothing."""
    with pytest.raises(AssertionError, match="still holding"):
        braking(entry={"paced": True}, clock=Clock(max_naps=100))


def test_the_wait_is_still_chunked(braking):
    """Chunking is what lets a policy edit reach a frozen agent, so the clamp
    must not collapse the hold into one long sleep."""
    clock, _reason = braking(entry={"paced": True, "max_delay": 60})
    assert len(clock.naps) > 1
    assert max(clock.naps) <= 15


def test_a_max_delay_shorter_than_the_chunk_is_not_overshot(braking):
    """The regression: clamping only the release check and not the sleep meant
    `--max-delay 5` under a 15s chunk still held for 15s."""
    clock, reason = braking(entry={"paced": True, "max_delay": 5})
    assert reason == "max_delay-release"
    assert clock.elapsed == pytest.approx(5, abs=1)


def test_max_delay_below_one_second_does_not_spin(braking):
    """The release check is >=, so a zero-length nap would busy-loop."""
    clock, reason = braking(entry={"paced": True, "max_delay": 0.1})
    assert reason == "max_delay-release"
    assert all(n >= 1.0 for n in clock.naps)


def test_a_blind_hold_is_released_and_labelled_distinctly(cwd, tmp_path,
                                                          monkeypatch):
    """Proceeding while blind is the loosest thing this tool ever does, so the
    log must say so rather than reading like an ordinary release."""
    policy_path = tmp_path / "policy.json"
    state_path = tmp_path / "state.json"
    policy_path.write_text(json.dumps(
        policy_for(cwd, entry={"paced": True, "max_delay": 30})), encoding="utf-8")
    state_path.write_text(json.dumps({"ts_epoch": HALFWAY - 9999, "buckets": {}}),
                          encoding="utf-8")
    monkeypatch.setattr(hook, "POLICY_PATH", str(policy_path))
    monkeypatch.setattr(hook, "STATE_PATH", str(state_path))
    monkeypatch.setattr(hook, "log", lambda msg: None)
    monkeypatch.setattr(hook, "refresh_snapshot", lambda: False)
    clock = Clock()
    monkeypatch.setattr(hook.time, "time", clock.time)
    monkeypatch.setattr(hook.time, "sleep", clock.sleep)

    _brake_start, reason = hook.run(cwd)
    assert reason == "max_delay-release-WHILE-BLIND"


def test_a_mid_brake_policy_edit_takes_effect(braking, cwd, tmp_path):
    """max_delay is re-read every cycle like every other knob, so raising it on
    a folder that is already frozen extends that hold rather than waiting for
    the agent's next tool call."""
    policy_path = tmp_path / "policy.json"
    clock = Clock()
    original_sleep = clock.sleep

    def sleep_then_raise_the_cap(seconds):
        original_sleep(seconds)
        if clock.elapsed >= 30:
            policy_path.write_text(json.dumps(
                policy_for(cwd, entry={"paced": True, "max_delay": 120})),
                encoding="utf-8")

    clock.sleep = sleep_then_raise_the_cap
    _clk, reason = braking(entry={"paced": True, "max_delay": 60}, clock=clock)
    assert reason == "max_delay-release"
    assert clock.elapsed == pytest.approx(120, abs=1)


# --- the setting round-trips through the CLI ---------------------------------

def test_cmd_on_persists_max_delay(tmp_path, monkeypatch, capsys):
    policy_path = tmp_path / "policy.json"
    monkeypatch.setattr(cli, "POLICY_PATH", str(policy_path))
    target = tmp_path / "proj"
    target.mkdir()

    cli.cmd_on(str(target), model="opus", m0=None, m1=None, fanout_reserve=None,
               enforce=None, max_delay=90)
    capsys.readouterr()

    saved = json.loads(policy_path.read_text(encoding="utf-8"))
    assert saved["paths"][norm_path(str(target))]["max_delay"] == 90


def test_no_max_delay_removes_the_cap(tmp_path, monkeypatch, capsys):
    policy_path = tmp_path / "policy.json"
    monkeypatch.setattr(cli, "POLICY_PATH", str(policy_path))
    target = tmp_path / "proj"
    target.mkdir()

    cli.cmd_on(str(target), model="opus", m0=None, m1=None, fanout_reserve=None,
               enforce=None, max_delay=90)
    cli.cmd_on(str(target), model=None, m0=None, m1=None, fanout_reserve=None,
               enforce=None, max_delay=None, no_max_delay=True)
    capsys.readouterr()

    entry = json.loads(policy_path.read_text(encoding="utf-8"))["paths"][
        norm_path(str(target))]
    assert entry["max_delay"] is None


def test_no_max_delay_beats_a_defaults_level_cap(tmp_path, monkeypatch, capsys):
    """A pop would inherit the default, so "off" would leave a cap on wherever
    one is configured globally. It has to write an explicit null."""
    policy_path = tmp_path / "policy.json"
    policy_path.write_text(json.dumps({
        "global": {"enabled": True},
        "defaults": {"m0": 5, "m1": 8, "chunk": 15, "max_delay": 300},
        "paths": {},
    }), encoding="utf-8")
    monkeypatch.setattr(cli, "POLICY_PATH", str(policy_path))
    target = tmp_path / "proj"
    target.mkdir()

    cli.cmd_on(str(target), model="opus", m0=None, m1=None, fanout_reserve=None,
               enforce=None, max_delay=None, no_max_delay=True)
    capsys.readouterr()

    pol = json.loads(policy_path.read_text(encoding="utf-8"))
    cwd = norm_path(str(target))
    d = hook.decide(pol, state_for({"session": session_bucket(HOT_PCT)}),
                    cwd, HALFWAY)
    assert d["braked"] is True
    assert d["max_delay"] is None


def test_cmd_on_leaves_max_delay_alone_when_not_given(tmp_path, monkeypatch,
                                                      capsys):
    """Re-running `on` to change the model must not silently drop the cap."""
    policy_path = tmp_path / "policy.json"
    monkeypatch.setattr(cli, "POLICY_PATH", str(policy_path))
    target = tmp_path / "proj"
    target.mkdir()

    cli.cmd_on(str(target), model="opus", m0=None, m1=None, fanout_reserve=None,
               enforce=None, max_delay=90)
    cli.cmd_on(str(target), model="fable", m0=None, m1=None, fanout_reserve=None,
               enforce=None, max_delay=None)
    capsys.readouterr()

    entry = json.loads(policy_path.read_text(encoding="utf-8"))["paths"][
        norm_path(str(target))]
    assert entry["max_delay"] == 90
    assert entry["model"] == "fable"
