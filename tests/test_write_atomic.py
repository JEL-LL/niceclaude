"""write_atomic on Windows: a refused replace, and the temp files it leaked.

On Windows os.replace onto a file another process has open fails with
PermissionError (WinError 5), and a hook reading state.json is such a process.
write_atomic used to raise there and leave `<path>.tmp.<pid>` behind -- 39 of
them accumulated in one live data dir -- and the snapshot went unpublished. It
now retries the replace briefly, removes its own temp file whenever the write
fails, and sweeps up the ones dead writers left.
"""

import os
import threading
import time

import pytest

from niceclaude import cli


@pytest.fixture
def target(tmp_path, monkeypatch):
    monkeypatch.setattr(cli, "REPLACE_RETRY_STEP", 0.001)
    path = tmp_path / "state.json"
    path.write_text("{}", encoding="utf-8")
    return path


def tmps(path):
    return sorted(n for n in os.listdir(path.parent)
                  if n.startswith(path.name + ".tmp."))


def refusing(times):
    """An os.replace that refuses `times` times, as Windows does while a
    reader holds the target, then behaves."""
    real, calls = os.replace, []

    def replace(src, dst):
        calls.append(src)
        if len(calls) <= times:
            raise PermissionError(13, "Access is denied")
        return real(src, dst)
    return replace, calls


def test_a_briefly_refused_replace_is_retried(target, monkeypatch):
    replace, calls = refusing(3)
    monkeypatch.setattr(cli.os, "replace", replace)
    cli.write_atomic(str(target), '{"new": 1}')
    assert target.read_text(encoding="utf-8") == '{"new": 1}'
    assert len(calls) == 4
    assert tmps(target) == []


def test_a_replace_refused_past_the_deadline_raises_and_cleans_up(
        target, monkeypatch):
    monkeypatch.setattr(cli, "REPLACE_RETRY_SECONDS", 0.05)
    replace, _ = refusing(10 ** 9)
    monkeypatch.setattr(cli.os, "replace", replace)
    with pytest.raises(PermissionError):
        cli.write_atomic(str(target), '{"new": 1}')
    assert target.read_text(encoding="utf-8") == "{}"
    assert tmps(target) == []


def test_other_errors_are_not_retried(target, monkeypatch):
    calls = []

    def replace(src, dst):
        calls.append(src)
        raise FileNotFoundError(2, "gone")
    monkeypatch.setattr(cli.os, "replace", replace)
    with pytest.raises(FileNotFoundError):
        cli.write_atomic(str(target), "{}")
    assert len(calls) == 1
    assert tmps(target) == []


def test_a_write_that_fails_before_the_replace_leaves_nothing(
        target, monkeypatch):
    def fsync(fd):
        raise OSError(28, "No space left on device")
    monkeypatch.setattr(cli.os, "fsync", fsync)
    with pytest.raises(OSError):
        cli.write_atomic(str(target), '{"new": 1}')
    assert target.read_text(encoding="utf-8") == "{}"
    assert tmps(target) == []


def age(path, seconds):
    t = time.time() - seconds
    os.utime(path, (t, t))


def test_only_old_temp_files_are_swept(target):
    """Age decides, not pid: a data dir shared across containers or hosts has
    pids that mean nothing here, so a young file may be a write in flight."""
    d = target.parent
    for name, secs in (("state.json.tmp.11", 3600), ("state.json.tmp.22", 5),
                       ("policy.json.tmp.11", 3600),        # not this file's
                       ("state.json.tmp.11.bak", 3600),     # not the pattern
                       ("state.json.tmp.notapid", 3600)):
        (d / name).write_text("x")
        age(d / name, secs)
    cli._sweep_stale_tmps(str(target))
    assert tmps(target) == ["state.json.tmp.11.bak", "state.json.tmp.22",
                            "state.json.tmp.notapid"]
    assert (d / "policy.json.tmp.11").exists()


def test_an_odd_name_never_breaks_the_sweep(target):
    for name in ("state.json.tmp.\u00b2", "state.json.tmp.99999999999999999"):
        (target.parent / name).write_text("x")
        age(target.parent / name, 3600)
    cli._sweep_stale_tmps(str(target))            # must not raise
    assert "state.json.tmp.\u00b2" in tmps(target)


def test_publishing_the_snapshot_sweeps(target, monkeypatch):
    monkeypatch.setattr(cli, "STATE_PATH", str(target))
    old = target.parent / "state.json.tmp.11"
    old.write_text("x")
    age(old, 3600)
    cli.publish_state({"ts_epoch": 1, "ts": "2026-10-05T00:00:00Z",
                       "exit_code": 0, "buckets": {}})
    assert tmps(target) == []


def test_other_writes_never_sweep(tmp_path):
    """write_atomic also writes Claude's own settings.json, in a directory
    niceclaude does not own, so it never sweeps by itself."""
    path = tmp_path / "settings.json"
    other = tmp_path / "settings.json.tmp.11"
    other.write_text("someone else's")
    age(other, 3600)
    cli.write_atomic(str(path), "{}")
    assert other.exists()


@pytest.mark.skipif(os.name != "nt", reason="only Windows refuses the replace")
def test_a_real_reader_holding_the_file_open(target):
    """The live failure: a handle open on the target, released shortly after,
    as a hook's read is."""
    fh = open(target, encoding="utf-8")
    closer = threading.Timer(0.2, fh.close)
    closer.start()
    try:
        cli.write_atomic(str(target), '{"new": 1}')
    finally:
        closer.join()
        fh.close()
    assert target.read_text(encoding="utf-8") == '{"new": 1}'
    assert tmps(target) == []


class _Stop(Exception):
    pass


def test_watch_survives_a_failed_publish(monkeypatch, capsys):
    rec = {"ts": "2026-10-05T00:00:00Z", "exit_code": 0, "buckets": {"x": 1},
           "unparsed_lines": [], "elapsed_ms": 0}
    monkeypatch.setattr(cli, "sample_once", lambda: dict(rec))
    monkeypatch.setattr(cli, "append_log", lambda r: None)

    def publish(r):
        raise PermissionError(13, "Access is denied")
    monkeypatch.setattr(cli, "publish_state", publish)

    def sleep(s):
        raise _Stop
    monkeypatch.setattr(cli.time, "sleep", sleep)
    with pytest.raises(_Stop):              # reached the sleep: still alive
        cli._watch_loop(60)
    assert "publish failed" in capsys.readouterr().err
