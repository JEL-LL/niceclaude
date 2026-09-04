"""What `status` says about *how long* a line will hold work.

Watching a frozen agent, the question is never "am I paced" -- the terminal
already answers that by sitting there. It is "how long, and which line". The
answer has to come from the same arithmetic the hook brakes on, or `status`
becomes a second opinion rather than a report, so the first assertion here is
an anti-drift one and the rest are about the reporting.

Every bucket is reported, including the ones the folder ignores. The weekly
line rises at 0.60 %/h against the session line at 20 %/h, so the two produce
waits that differ by orders of magnitude; seeing all of them is how you tell
whether `--enforce` is set the way you meant.
"""

import json

import pytest

from niceclaude import cli, hook
from niceclaude._shared import bucket_pace, norm_path

SESSION_WINDOW = 5 * 3600
WEEK_WINDOW = 7 * 86400
RESETS = 1_760_000_000                       # fixed epoch; never the real clock
START = RESETS - SESSION_WINDOW
HALFWAY = START + SESSION_WINDOW // 2        # f_t = 0.5, so allowed = 48.5


def session_bucket(pct, resets=RESETS, window=SESSION_WINDOW):
    return {"pct": pct, "resets_epoch": resets, "window_seconds": window,
            "label": None}


def week_bucket(pct, now=HALFWAY, elapsed_fraction=0.5):
    start = now - int(WEEK_WINDOW * elapsed_fraction)
    return {"pct": pct, "resets_epoch": start + WEEK_WINDOW,
            "window_seconds": WEEK_WINDOW, "label": None}


# --- the report and the brake must not drift ---------------------------------

def test_bucket_pace_wake_is_the_wake_the_hook_brakes_to(tmp_path):
    """`status` reports bucket_pace; the hook brakes on hook.decide. A second
    implementation of the pace line is how the two would start disagreeing."""
    cwd = norm_path(str(tmp_path))
    pol = {"paths": {cwd: {"paced": True, "model": "opus"}},
           "defaults": {"m0": 5, "m1": 8, "chunk": 15}}
    st = {"ts_epoch": HALFWAY, "buckets": {"session": session_bucket(90)}}
    d = hook.decide(pol, st, cwd, HALFWAY)
    p = bucket_pace(st["buckets"]["session"], HALFWAY, 5, 8)
    assert d["braked"] is True
    assert p["wake"] == pytest.approx(d["wake_at"])
    assert p["wait"] == pytest.approx(d["wake_at"] - HALFWAY)


def test_a_bucket_under_the_line_reports_no_wait():
    p = bucket_pace(session_bucket(10), HALFWAY, 5, 8)
    assert p["over"] is False
    assert p["wait"] is None


def test_a_bucket_with_no_reset_clause_has_no_solvable_wait():
    """Right after a window rolls the server omits the reset clause. There is
    no line to solve, so there is no honest number to print."""
    b = {"pct": 40, "resets_epoch": None, "window_seconds": SESSION_WINDOW,
         "label": None}
    p = bucket_pace(b, HALFWAY, 5, 8)
    assert p["over"] is True          # judged against the m0 floor
    assert p["wait"] is None


def test_a_nonsensical_span_does_not_divide_by_zero():
    p = bucket_pace(session_bucket(90), HALFWAY, 60, 60)
    assert p["over"] is True
    assert p["wait"] is not None


# --- rendering ---------------------------------------------------------------

@pytest.mark.parametrize("seconds,text", [
    (0,        "0s"),
    (42,       "42s"),
    (60,       "1m00s"),
    (90.4,     "1m31s"),          # rounded up
    (3600,     "1h00m"),
    (6180,     "1h43m"),
    (86400,    "1d00h"),
    (431_520,  "5d00h"),          # 119h52m, rounded up to the hour
])
def test_human_delta_rounds_up_at_every_scale(seconds, text):
    assert cli.human_delta(seconds) == text


def test_an_ignored_line_still_reports_the_wait_it_would_produce():
    p = bucket_pace(session_bucket(90), HALFWAY, 5, 8)
    assert cli.describe_hold(p, enforced=True, chunk=15).startswith("HOLDS ")
    assert cli.describe_hold(p, enforced=False, chunk=15).startswith("would hold ")


def test_an_unsolvable_hold_says_so_rather_than_printing_a_number():
    b = {"pct": 40, "resets_epoch": None, "window_seconds": SESSION_WINDOW,
         "label": None}
    text = cli.describe_hold(bucket_pace(b, HALFWAY, 5, 8), True, 15)
    assert "unsolvable" in text and "15s" in text


# --- the whole command -------------------------------------------------------

@pytest.fixture
def paced(tmp_path, monkeypatch):
    """A paced folder, a snapshot, and a clock frozen at HALFWAY."""
    monkeypatch.setattr(cli, "POLICY_PATH", str(tmp_path / "policy.json"))
    monkeypatch.setattr(cli, "STATE_PATH", str(tmp_path / "state.json"))
    monkeypatch.setattr(cli.time, "time", lambda: float(HALFWAY))
    work = tmp_path / "work"
    work.mkdir()
    cwd = norm_path(str(work))

    def setup(buckets, enforce="session,week,model", fanout_reserve=0,
              ts_epoch=HALFWAY - 10):
        entry = {"paced": True, "model": "opus", "enforce": enforce}
        if fanout_reserve:
            entry["fanout_reserve"] = fanout_reserve
        cli.save_policy({"global": {"enabled": True},
                         "defaults": {"m0": 5, "m1": 8, "chunk": 15},
                         "paths": {cwd: entry}})
        cli.write_atomic(cli.STATE_PATH, json.dumps(
            {"ts_epoch": ts_epoch, "ok": True, "buckets": buckets}))
        return cwd

    return setup


def test_status_names_the_line_and_the_wait(paced, capsys):
    cwd = paced({"session": session_bucket(90),
                 "week:all models": week_bucket(4)})
    assert cli.cmd_status(cwd) == 0
    out = capsys.readouterr().out
    assert "BRAKED -- session 90% over line 48.5%" in out

    with open(cli.STATE_PATH, encoding="utf-8") as fh:
        st = json.load(fh)
    wake = hook.decide(cli.load_policy(), st, cwd, HALFWAY)["wake_at"]
    assert f"releases in {cli.human_delta(wake - HALFWAY)}" in out
    assert "next check in 15s" in out


def test_status_reports_every_line_including_the_ignored_ones(paced, capsys):
    """The motivating request: print what each line would cost and let the
    reader decide which one is the pertinent one."""
    cwd = paced({"session": session_bucket(90),
                 "week:all models": week_bucket(90)}, enforce="session")
    cli.cmd_status(cwd)
    rows = capsys.readouterr().out.splitlines()
    session_row = next(r for r in rows if r.startswith("  session"))
    week_row = next(r for r in rows if r.startswith("  week:all models"))
    assert "ENFORCED" in session_row and "HOLDS " in session_row
    assert "ignored" in week_row and "would hold " in week_row
    # ...and the ignored weekly line is the far more expensive one, which is
    # the whole reason for printing a line the folder does not answer to.
    assert "d" in week_row.split("would hold ")[1]      # days, not minutes


def test_status_says_running_when_nothing_enforced_is_over(paced, capsys):
    cwd = paced({"session": session_bucket(4)}, enforce="session")
    cli.cmd_status(cwd)
    out = capsys.readouterr().out
    assert "running -- no enforced line is over" in out
    assert "clear" in out


def test_status_reports_the_stricter_line_a_spawn_faces(paced, capsys):
    cwd = paced({"session": session_bucket(90)}, enforce="session",
                fanout_reserve=10)
    cli.cmd_status(cwd)
    assert "SubagentStart is held to m1 8+10" in capsys.readouterr().out


def test_status_does_not_claim_a_release_it_cannot_solve(paced, capsys):
    """A stale snapshot brakes blind. There is no wake time to solve for, and
    inventing one would be the same lie as a wrong ETA."""
    cwd = paced({"session": session_bucket(4)}, enforce="session",
                ts_epoch=HALFWAY - 4000)
    cli.cmd_status(cwd)
    out = capsys.readouterr().out
    assert "BRAKED, blind" in out
    assert "releases in" not in out
    assert "re-checks every 15s" in out
