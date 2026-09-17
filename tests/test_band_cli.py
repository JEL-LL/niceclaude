"""The two band knobs at the CLI: writing them, and reporting three regions.

The band exists because releasing a hold exactly at the pace line resumes with
under one quantum of headroom, so the next 1% tick brakes again -- on the
weekly line that is a 1.68h hold for every 1% of budget, each one long enough
to kill the prompt cache. A second line below the first makes one hold buy a
whole band of running instead of one quantum.

`_shared.bucket_pace` and `hook.decide` already implement that. What is tested
here is the part a person touches: that `niceclaude on` records the two
settings the way the rest of the policy is recorded, and that `status` reports
which of the three regions a folder is in using the same arithmetic the hook
brakes on. `status` disagreeing with the hook is the failure mode this project
is most careful about -- a report that is a second opinion is worse than no
report, because it is believed.

The clock is frozen throughout. At f_t = 0.5 with m0 5 and m1 8 the brake line
is at 48.5%, so a band of 2 puts the throttle line at 46.5% and the three
regions are reachable with three whole percentages.
"""

import json

import pytest

from niceclaude import cli, hook
from niceclaude._shared import bucket_pace, norm_path

SESSION_WINDOW = 5 * 3600
RESETS = 1_760_000_000                       # fixed epoch; never the real clock
START = RESETS - SESSION_WINDOW
HALFWAY = START + SESSION_WINDOW // 2        # f_t = 0.5, so allowed = 48.5

BAND = 2                                     # throttle line at 46.5
# pess is pct + 1, so these three land one per region against those two lines.
FREE_PCT = 45                                # pess 46, under 46.5
BAND_PCT = 47                                # pess 48, between 46.5 and 48.5
OVER_PCT = 90                                # pess 91, and hours from clearing


def session_bucket(pct):
    return {"pct": pct, "resets_epoch": RESETS, "window_seconds": SESSION_WINDOW,
            "label": None}


# --- the flags reach policy.json ---------------------------------------------

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


def on(target, **kwargs):
    """`cmd_on` with everything not under test left alone."""
    fields = dict(model=None, m0=None, m1=None, fanout_reserve=None,
                  enforce=None, max_delay=None)
    fields.update(kwargs)
    return cli.cmd_on(str(target), **fields)


def test_cmd_on_persists_the_band_and_its_delay(policy, capsys):
    target, entry, _path = policy
    on(target, model="opus", band=2, band_delay=30)
    capsys.readouterr()
    assert entry()["band"] == 2
    assert entry()["band_delay"] == 30


def test_band_zero_is_recorded_rather_than_dropped(policy, capsys):
    """0 is how the band is turned off, which is why there is no --no-band.

    That only works if a zero is written. Testing `if band:` instead of
    `if band is not None:` would make `--band 0` a no-op, and on a policy with
    a defaults-level band the folder would silently keep the band it was just
    told to give up -- the same falsy-vs-missing trap the pace-line code is
    written around.
    """
    target, entry, path = policy
    # A default band of 3 puts the throttle line at 45.5, so FREE_PCT is in
    # the band under the default and free once the folder overrides it to 0.
    defaults = {"m0": 5, "m1": 8, "chunk": 15, "band": 3}
    path.write_text(json.dumps({"global": {"enabled": True},
                                "defaults": defaults, "paths": {}}),
                    encoding="utf-8")
    state = {"ts_epoch": HALFWAY,
             "buckets": {"session": session_bucket(FREE_PCT)}}
    cwd = norm_path(str(target))

    on(target, model="opus", band=0)
    capsys.readouterr()
    assert entry()["band"] == 0

    pol = json.loads(path.read_text(encoding="utf-8"))
    assert hook.decide(pol, state, cwd, HALFWAY)["region"] == "free"
    # ...and it is the written zero doing that, not the reading being free
    # anyway: the same folder without it inherits the default and is throttled.
    inherited = {"global": {"enabled": True}, "defaults": defaults,
                 "paths": {cwd: {"paced": True, "model": "opus"}}}
    assert hook.decide(inherited, state, cwd, HALFWAY)["region"] == "band"


def test_no_band_delay_beats_a_defaults_level_band_delay(policy, capsys):
    """Popping the key would inherit the default, so "run the band at full
    speed" would leave this folder in a lower gear wherever one is configured
    globally. It has to write an explicit null, exactly as --no-max-delay does.
    """
    target, entry, path = policy
    path.write_text(json.dumps({
        "global": {"enabled": True},
        "defaults": {"m0": 5, "m1": 8, "chunk": 15, "band": 2, "band_delay": 30},
        "paths": {},
    }), encoding="utf-8")

    on(target, model="opus", no_band_delay=True)
    capsys.readouterr()
    assert entry()["band_delay"] is None

    cwd = norm_path(str(target))
    d = hook.decide(json.loads(path.read_text(encoding="utf-8")),
                    {"ts_epoch": HALFWAY,
                     "buckets": {"session": session_bucket(BAND_PCT)}},
                    cwd, HALFWAY)
    assert d["region"] == "band"
    assert d["band_delay"] is None
    assert d["braked"] is False      # the band is hysteresis again, not a gear


def test_cmd_on_leaves_the_band_alone_when_not_given(policy, capsys):
    """Re-running `on` to change one setting must not quietly drop the rest."""
    target, entry, _path = policy
    on(target, model="opus", band=2, band_delay=30)
    on(target, model="fable")
    capsys.readouterr()
    assert entry()["band"] == 2
    assert entry()["band_delay"] == 30
    assert entry()["model"] == "fable"


def test_the_flags_are_wired_to_cmd_on(policy, capsys):
    """Through `main`, so the argparse dest names are covered too. A typo in a
    dest raises only at run time, which is after the flag has looked accepted.
    """
    target, entry, _path = policy
    assert cli.main(["on", str(target), "--model", "opus",
                     "--band", "2.5", "--band-delay", "45"]) == 0
    capsys.readouterr()
    assert entry()["band"] == 2.5
    assert entry()["band_delay"] == 45


def test_setting_and_clearing_the_band_delay_at_once_is_refused(policy, capsys):
    """One of them has to lose, and silently picking a winner is how a config
    ends up meaning something nobody wrote."""
    target, _entry, _path = policy
    with pytest.raises(SystemExit) as exc:
        cli.main(["on", str(target), "--band-delay", "30", "--no-band-delay"])
    assert exc.value.code == 2
    assert "not allowed with" in capsys.readouterr().err


# --- status reports three regions --------------------------------------------

@pytest.fixture
def paced(tmp_path, monkeypatch):
    """A paced folder, a snapshot, and a clock frozen at HALFWAY."""
    monkeypatch.setattr(cli, "POLICY_PATH", str(tmp_path / "policy.json"))
    monkeypatch.setattr(cli, "STATE_PATH", str(tmp_path / "state.json"))
    monkeypatch.setattr(cli.time, "time", lambda: float(HALFWAY))
    work = tmp_path / "work"
    work.mkdir()
    cwd = norm_path(str(work))

    def setup(pct, band=BAND, band_delay=None, max_delay=None,
              ts_epoch=HALFWAY - 10):
        entry = {"paced": True, "model": "opus", "enforce": "session",
                 "band": band}
        if band_delay is not None:
            entry["band_delay"] = band_delay
        if max_delay is not None:
            entry["max_delay"] = max_delay
        cli.save_policy({"global": {"enabled": True},
                         "defaults": {"m0": 5, "m1": 8, "chunk": 15},
                         "paths": {cwd: entry}})
        cli.write_atomic(cli.STATE_PATH, json.dumps(
            {"ts_epoch": ts_epoch, "ok": True,
             "buckets": {"session": session_bucket(pct)}}))
        return cwd

    return setup


def test_status_prints_the_band_next_to_max_delay(paced, capsys):
    """They are read against each other: max_delay buys cache warmth by
    spending the ceiling, the band buys it below the ceiling. Printing one
    without the other leaves the reader unable to tell which is paying."""
    cli.cmd_status(paced(FREE_PCT, band_delay=30))
    out = capsys.readouterr().out
    assert "  band          2% under the brake line" in out
    assert "  band_delay    30s per tool call" in out


def test_an_unset_band_delay_says_the_band_is_hysteresis_only(paced, capsys):
    """"none" alone would read as a missing setting rather than a deliberate
    one; unset is a working configuration with its own behaviour."""
    cli.cmd_status(paced(FREE_PCT))
    assert "release hysteresis only" in capsys.readouterr().out


def test_status_reports_the_throttled_region(paced, capsys):
    """Between the lines is neither of the two states `status` used to have.
    Calling it BRAKED would say work has stopped when it has not, and calling
    it running would hide a hold on every tool call.
    """
    cli.cmd_status(paced(BAND_PCT, band_delay=30))
    out = capsys.readouterr().out
    assert "THROTTLED -- session 47% in band" in out
    assert "throttle 46.5%" in out
    assert "holds 30s per tool call" in out
    assert "not a stop" in out


def test_the_resume_time_is_the_hooks_own_wake_time(paced, capsys):
    """The anti-drift assertion, and the reason the band arithmetic lives in
    _shared: a throttled agent resumes full speed when the THROTTLE line
    catches up, and `status` must not solve that for itself and get a
    different answer from the hook that is holding it."""
    cwd = paced(BAND_PCT, band_delay=30)
    cli.cmd_status(cwd)
    out = capsys.readouterr().out

    with open(cli.STATE_PATH, encoding="utf-8") as fh:
        st = json.load(fh)
    d = hook.decide(cli.load_policy(), st, cwd, HALFWAY)
    assert d["region"] == "band"
    assert f"Full speed resumes in {cli.human_delta(d['wake_at'] - HALFWAY)}" in out


def test_a_brake_says_it_runs_down_to_the_throttle_line(paced, capsys):
    """Both braking regions aim at the throttle line -- the brake line only
    decides whether the hold is capped. Reporting the brake line as the target
    would understate the wait, and worse, would describe the old behaviour the
    band was added to replace.
    """
    cwd = paced(OVER_PCT, band_delay=30)
    cli.cmd_status(cwd)
    out = capsys.readouterr().out
    assert "BRAKED -- session 90% over line 48.5%" in out
    assert "runs down to the throttle line, 2% below the brake" in out

    with open(cli.STATE_PATH, encoding="utf-8") as fh:
        st = json.load(fh)
    wake = hook.decide(cli.load_policy(), st, cwd, HALFWAY)["wake_at"]
    assert f"releases in {cli.human_delta(wake - HALFWAY)}" in out


def test_a_capped_brake_names_the_throttle_line_as_what_clears(paced, capsys):
    """max_delay releases while still over the line, so the solved time is the
    one thing it is honest to report -- and with a band that time is when the
    THROTTLE line arrives, which is later than the brake line's."""
    cli.cmd_status(paced(OVER_PCT, band_delay=30, max_delay=240))
    out = capsys.readouterr().out
    assert "holds 4m00s (max_delay)" in out
    assert "the throttle line 2% under it clears in" in out


def test_a_band_with_no_band_delay_reads_as_running(paced, capsys):
    """Above the throttle line with nothing holding is a real state, and one
    that looks like a bug unless it is named. The band is then pure release
    hysteresis: free to cross, and it only deepens the hold the brake line
    triggers."""
    cli.cmd_status(paced(BAND_PCT))
    out = capsys.readouterr().out
    assert "running -- between the lines" in out
    assert "pure release hysteresis" in out
    assert "THROTTLED" not in out


def test_the_table_prints_both_lines_when_a_band_is_configured(paced, capsys):
    """One number cannot say which of three regions a bucket is in."""
    cli.cmd_status(paced(BAND_PCT, band_delay=30))
    row = next(r for r in capsys.readouterr().out.splitlines()
               if r.startswith("  session"))
    p = bucket_pace(session_bucket(BAND_PCT), HALFWAY, 5, 8, BAND)
    assert f"lines {p['low']:5.1f}/{p['allowed']:5.1f}%" in row
    assert "THROTTLES 30s/call" in row


def test_the_table_reports_the_hold_max_delay_actually_leaves(paced, capsys):
    """Inside the band the cap is min(band_delay, max_delay), so quoting
    band_delay alone would print five minutes for a hold of one. The table
    reports what the hook does, not which knob the number came from -- and the
    verdict below it names the knob, because a number the reader cannot trace
    back to a setting is not actionable."""
    cli.cmd_status(paced(BAND_PCT, band_delay=300, max_delay=60))
    out = capsys.readouterr().out
    row = next(r for r in out.splitlines() if r.startswith("  session"))
    assert "THROTTLES 1m00s/call" in row
    assert "holds 1m00s per tool call" in out
    assert "band_delay is 5m00s; max_delay caps it here" in out


def test_band_delay_is_not_offered_as_an_escape_from_a_blind_hold(paced, capsys):
    """A degraded snapshot is reported as `over`, deliberately: "I cannot see"
    must not resolve to "I am comfortably under the brake line", which is what
    releasing it after one band_delay would assert. Someone watching a long
    hold with band_delay set will assume the knob is broken, so say it."""
    cli.cmd_status(paced(FREE_PCT, band_delay=30, ts_epoch=HALFWAY - 4000))
    out = capsys.readouterr().out
    assert "BRAKED, blind" in out
    assert "band_delay does not apply while blind" in out
    assert "THROTTLED" not in out


def test_without_a_band_the_report_is_what_it_always_was(paced, capsys):
    """band 0 is exactly the previous behaviour, and the report has to be too.
    Two lines where there is only one would be a false reading of the policy,
    and everyone who has not set a band would see it."""
    cli.cmd_status(paced(OVER_PCT, band=0))
    out = capsys.readouterr().out
    assert "  band          0 -- brake line only" in out
    assert "line  48.5% " in out and "lines " not in out
    assert "BRAKED -- session 90% over line 48.5%" in out
    assert "throttle" not in out
    assert "THROTTLED" not in out


# --- the row's own phrasing ---------------------------------------------------

def test_a_throttle_on_an_ignored_line_is_priced_as_the_hypothetical_it_is():
    """Same distinction the braking rows draw: "would throttle" is a line you
    could switch on with --enforce, "THROTTLES" is one slowing you now."""
    p = bucket_pace(session_bucket(BAND_PCT), HALFWAY, 5, 8, BAND)
    assert p["region"] == "band"
    assert cli.describe_hold(p, True, 15, 30).startswith("THROTTLES ")
    assert cli.describe_hold(p, False, 15, 30).startswith("would throttle ")


def test_a_band_with_no_hold_is_not_reported_as_a_wait():
    """`p` still carries the solved release, so printing it here would quote
    hours for a bucket nothing is holding."""
    p = bucket_pace(session_bucket(BAND_PCT), HALFWAY, 5, 8, BAND)
    assert p["wait"] is not None
    text = cli.describe_hold(p, True, 15, None)
    assert text.startswith("clear")
    assert "no band_delay" in text
