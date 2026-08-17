"""The tools must say when their own input is untrustworthy.

Without `niceclaude watch` running, the only thing appending to usage.jsonl is
the hook's on-demand refresh — which fires at most every MAX_STALE seconds, and
only on paced folders while work is actually happening. Idle time is then
entirely absent from the record, so every "average" burn rate is overstated.
Reporting that number without comment would be worse than not reporting it.
"""

import json
import time

import pytest

from niceclaude import cli


@pytest.fixture
def log(tmp_path, monkeypatch):
    def write(interval_seconds, n=20):
        now = int(time.time())
        reset = time.strftime("%b %d, %I:%M%p", time.gmtime(now + 3600))
        path = tmp_path / "usage.jsonl"
        with open(path, "w", encoding="utf-8") as fh:
            for i in range(n):
                fh.write(json.dumps({
                    "ts": "x", "ts_epoch": now - (n - i) * interval_seconds,
                    "exit_code": 0, "elapsed_ms": 1, "stderr": None,
                    "raw": f"Current session: {i}% used \u00b7 resets {reset} (UTC)\n",
                    "buckets": {}, "unparsed_lines": [],
                }) + "\n")
        monkeypatch.setattr(cli, "LOG_PATH", str(path))
    return write


def test_continuous_sampling_is_reported_without_warning(log, capsys):
    log(interval_seconds=60)          # what the daemon produces
    cli.cmd_burn(15)
    out = capsys.readouterr().out
    assert "median sampling interval: 60s" in out
    assert "WARNING" not in out


def test_activity_driven_sampling_is_flagged(log, capsys):
    log(interval_seconds=600)         # what on-demand refresh alone produces
    cli.cmd_burn(15)
    out = capsys.readouterr().out
    assert "median sampling interval: 600s" in out
    assert "WARNING" in out
    assert "overstated" in out


@pytest.mark.parametrize("interval,warns", [(60, False), (120, False),
                                            (180, True), (900, True)])
def test_warning_threshold(log, capsys, interval, warns):
    log(interval_seconds=interval)
    cli.cmd_burn(15)
    assert ("WARNING" in capsys.readouterr().out) is warns
