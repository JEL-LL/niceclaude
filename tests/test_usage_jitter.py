"""Reported usage is only approximately monotonic within a window."""

import json
import time

import pytest

from niceclaude import cli


@pytest.fixture
def log(tmp_path, monkeypatch):
    def write(pcts):
        now = int(time.time())
        reset = time.strftime("%b %d, %I:%M%p", time.gmtime(now + 3600))
        path = tmp_path / "usage.jsonl"
        with open(path, "w", encoding="utf-8") as fh:
            for i, pct in enumerate(pcts):
                fh.write(json.dumps({
                    "ts": f"t{i}", "ts_epoch": now - (len(pcts) - i) * 60,
                    "exit_code": 0, "elapsed_ms": 1, "stderr": None,
                    "raw": f"Current session: {pct}% used \u00b7 resets {reset} (UTC)\n",
                    "buckets": {}, "unparsed_lines": [],
                }) + "\n")
        monkeypatch.setattr(cli, "LOG_PATH", str(path))
    return write


def test_one_point_drop_is_tolerated(log, capsys):
    """Observed live: 4 -> 3 -> 6 with an unchanged reset time. The renderer
    floors a float, so a tiny backend recalculation crossing an integer
    boundary looks like a decrease."""
    log([4, 4, 3, 6, 6])
    rc = cli.cmd_check()
    out = capsys.readouterr().out
    assert rc == 0, out
    assert "no anomalies" in out


@pytest.mark.parametrize("pcts", [[9, 5], [40, 12], [80, 60]])
def test_larger_drops_are_still_reported(log, capsys, pcts):
    """A multi-point fall cannot be rounding, so it still means the parser is
    wrong -- which is the whole reason this assertion exists."""
    log(pcts)
    rc = cli.cmd_check()
    out = capsys.readouterr().out
    assert rc == 1
    assert "DECREASED" in out


def test_a_run_of_single_point_drops_still_trips(log, capsys):
    """Jitter is noise around a value; a sustained slide is not.

    Comparing each sample only to its predecessor tolerates a run of 1-point
    drops forever, so the comparison is against the window's running maximum.
    """
    log([20, 19, 18, 17, 16])
    rc = cli.cmd_check()
    assert rc == 1, capsys.readouterr().out


def test_a_drop_to_zero_reads_as_a_window_roll(log, capsys):
    """Usage is only 0 at the start of a window, so 80 -> 0 is a reset, not a
    misparse -- even though the reset label has not caught up yet."""
    log([80, 0, 1])
    rc = cli.cmd_check()
    assert rc == 0, capsys.readouterr().out
