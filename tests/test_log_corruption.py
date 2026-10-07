"""A bad line in usage.jsonl costs that line, and nothing else.

Two appends that collide can leave a fragment behind (open-questions 14). The
live log has one: an 8-byte `: true}` repeating the tail of the record before
it. Nothing locks the log, so the reader is what holds the invariant, and these
pin it: no fragment, torn byte or stray scalar may stop `load_log`, or the
`check` and `burn` built on it, from reading the rest.
"""

import json
import time

import pytest

from niceclaude import cli

MIDDLE_DOT = chr(0x00B7)   # /usage's separator: the multi-byte char a tear splits


def record(i, pct, now):
    reset = time.strftime("%b %d, %I:%M%p", time.gmtime(now + 3600))
    return {
        "ts": f"t{i}", "ts_epoch": now - (10 - i) * 60,
        "exit_code": 0, "elapsed_ms": 1, "stderr": None,
        "raw": f"Current session: {pct}% used {MIDDLE_DOT} resets "
               f"{reset} (UTC)\n",
        "buckets": {}, "unparsed_lines": [],
    }


@pytest.fixture
def log(tmp_path, monkeypatch):
    """Write usage.jsonl from raw byte lines, and point cli at it. Good records
    are written as the daemon writes them: UTF-8, ensure_ascii=False."""
    path = tmp_path / "usage.jsonl"
    monkeypatch.setattr(cli, "LOG_PATH", str(path))
    now = int(time.time())

    def good(i, pct):
        return json.dumps(record(i, pct, now), ensure_ascii=False).encode()

    def write(lines):
        path.write_bytes(b"".join(line + b"\n" for line in lines))
    return good, write


def test_the_fragment_seen_live_is_skipped(log, capsys):
    good, write = log
    write([good(0, 10), b": true}", good(1, 11)])
    got = cli.load_log()
    assert [r["ts"] for r in got] == ["t0", "t1"]
    assert "corrupt JSON at log line 2" in capsys.readouterr().err


def test_a_torn_multibyte_character_costs_only_its_line(log, capsys):
    """The middle dot is two bytes in UTF-8. A fragment that ends between
    them is invalid UTF-8; a strict decode raised out of the loop and lost
    every record, the good ones after it included."""
    good, write = log
    torn = good(1, 11)
    cut = torn.index(MIDDLE_DOT.encode()) + 1   # mid-character
    write([good(0, 10), torn[:cut], torn[cut:], good(2, 12)])
    got = cli.load_log()
    assert [r["ts"] for r in got] == ["t0", "t2"]
    # And a good record still carries its separator intact.
    assert MIDDLE_DOT in got[1]["raw"]
    err = capsys.readouterr().err
    assert "log line 2" in err and "log line 3" in err


@pytest.mark.parametrize("scalar", [b"true", b"5", b'"x"', b"null",
                                    b"[1, 2]"])
def test_a_line_that_parses_to_a_non_record_is_skipped(log, capsys, scalar):
    """Valid JSON is not a record. A tear can leave a bare `true` or number,
    and every caller indexes what load_log returns as a dict."""
    good, write = log
    write([good(0, 10), scalar, good(1, 11)])
    got = cli.load_log()
    assert [r["ts"] for r in got] == ["t0", "t1"]
    assert "log line 2 (not a record)" in capsys.readouterr().err


def test_check_and_burn_run_through_every_kind_of_damage(log, capsys):
    """End to end: the commands that read the log finish, rather than raising
    on the first bad line."""
    good, write = log
    torn = good(3, 13)
    cut = torn.index(MIDDLE_DOT.encode()) + 1
    write([good(0, 10), b": true}", good(1, 11), b"true", good(2, 12),
           torn[:cut], good(4, 14)])
    cli.cmd_check()
    cli.cmd_burn(15)
    assert "corrupt JSON" in capsys.readouterr().err
