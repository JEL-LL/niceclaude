"""load_log keeps this account's history, and only drops another account's.

Two accounts sharing one NICECLAUDE_DIR share one usage.jsonl. Read whole,
`check` sees one account's 80% followed by the other's 30% and calls it a
misparse, and `burn` and `plot` difference across accounts. So a record stamped
with another key is skipped, and counted on stderr.

An unstamped record is kept under every key. It predates the stamp, and history
wrongly hidden can never be re-attributed, so this is deliberately looser than
the snapshot rule (test_account_stamp), where a wrong trust only costs a
refresh.
"""

import json
import time

import pytest

from niceclaude import cli

pytestmark = pytest.mark.real_account_key

KEY_A = "/accounts/a/.claude-work"
KEY_B = "/accounts/b/.claude-home"
UNSTAMPED = ...


@pytest.fixture
def log(tmp_path, monkeypatch):
    """Write usage.jsonl from (stamp, pct) pairs, one minute apart, all in one
    session window, and point cli at it."""
    def write(rows):
        now = int(time.time())
        reset = time.strftime("%b %d, %I:%M%p", time.gmtime(now + 3600))
        path = tmp_path / "usage.jsonl"
        with open(path, "w", encoding="utf-8") as fh:
            for i, (stamp, pct) in enumerate(rows):
                rec = {
                    "ts": f"t{i}", "ts_epoch": now - (len(rows) - i) * 60,
                    "exit_code": 0, "elapsed_ms": 1, "stderr": None,
                    "raw": f"Current session: {pct}% used \u00b7 resets "
                           f"{reset} (UTC)\n",
                    "buckets": {}, "unparsed_lines": [],
                }
                if stamp is not UNSTAMPED:
                    rec["config_key"] = stamp
                fh.write(json.dumps(rec) + "\n")
        monkeypatch.setattr(cli, "LOG_PATH", str(path))
    return write


# Interleaved: A climbs 10 -> 30, B sits at 80-90, and two unstamped records
# from before the upgrade sit at the start.
ROWS = [(UNSTAMPED, 5), (UNSTAMPED, 6), (KEY_A, 10), (KEY_B, 80), ("", 40),
        (KEY_A, 20), (KEY_B, 90), (KEY_A, 30)]


def stamps(records):
    return [r.get("config_key", UNSTAMPED) for r in records]


def test_key_a_keeps_its_own_and_every_unstamped_record(log, monkeypatch,
                                                         capsys):
    monkeypatch.setattr(cli, "ACCOUNT_KEY", KEY_A)
    log(ROWS)
    got = cli.load_log()
    assert stamps(got) == [UNSTAMPED, UNSTAMPED, KEY_A, KEY_A, KEY_A]
    # B's two records and the default account's one.
    assert "skipped 3 records from other accounts" in capsys.readouterr().err


def test_the_default_key_keeps_its_own_and_every_unstamped_record(
        log, monkeypatch, capsys):
    monkeypatch.setattr(cli, "ACCOUNT_KEY", "")
    log(ROWS)
    got = cli.load_log()
    assert stamps(got) == [UNSTAMPED, UNSTAMPED, ""]
    assert "skipped 5 records from other accounts" in capsys.readouterr().err


def test_nothing_skipped_says_nothing(log, monkeypatch, capsys):
    monkeypatch.setattr(cli, "ACCOUNT_KEY", KEY_A)
    log([(UNSTAMPED, 5), (KEY_A, 10)])
    assert len(cli.load_log()) == 2
    assert "skipped" not in capsys.readouterr().err


def test_check_sees_no_false_decrease_across_the_skipped_records(
        log, monkeypatch, capsys):
    """Unfiltered, B's 90% followed by A's 30% is a 60-point fall inside one
    window -- exactly what `check` exists to call a misparse."""
    monkeypatch.setattr(cli, "ACCOUNT_KEY", KEY_A)
    log([(KEY_A, 10), (KEY_B, 80), (KEY_A, 20), (KEY_B, 90), (KEY_A, 30)])
    rc = cli.cmd_check()
    captured = capsys.readouterr()
    assert rc == 0, captured.out
    assert "DECREASED" not in captured.out
    assert "3 samples" in captured.out
    assert "skipped 2 records from other accounts" in captured.err


def test_the_same_log_unfiltered_would_have_tripped(log, monkeypatch, capsys):
    """The control: it is the filter, not the data, that keeps check quiet."""
    monkeypatch.setattr(cli, "ACCOUNT_KEY", KEY_A)
    log([(KEY_A, 10), (UNSTAMPED, 80), (KEY_A, 20)])
    assert cli.cmd_check() == 1
    assert "DECREASED" in capsys.readouterr().out
