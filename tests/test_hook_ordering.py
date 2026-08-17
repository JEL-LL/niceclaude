"""Regression tests for the ORDER of work in hook.run().

These exist because the existing suite tests `decide()` directly and therefore
could not catch a bug in `run()`: it refreshed the usage snapshot *before*
checking whether the folder was paced at all. Unpaced folders — every foreground
session, on every tool call — paid a ~2s `claude -p /usage` subprocess whenever
the snapshot was stale, which is the normal state whenever the hook is installed
without a daemon running.

Caught by tests/smoke_installed.py measuring 2056ms where 20ms was expected. The
lesson is in the assertions below: what matters is not only what `run()` decides
but what it *touches* on the way there.
"""

import json
import time

import pytest

from niceclaude import hook


@pytest.fixture
def state_dir(tmp_path, monkeypatch):
    """Point the hook's module-level paths at a temp dir."""
    monkeypatch.setattr(hook, "POLICY_PATH", str(tmp_path / "policy.json"))
    monkeypatch.setattr(hook, "STATE_PATH", str(tmp_path / "state.json"))
    monkeypatch.setattr(hook, "HOOK_LOG_PATH", str(tmp_path / "hook.log"))
    return tmp_path


def write(path, obj):
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(obj, fh)


@pytest.fixture
def no_refresh_allowed(monkeypatch):
    """Make any refresh attempt an explicit test failure."""
    calls = []

    def boom():
        calls.append(1)
        raise AssertionError("run() attempted a snapshot refresh")

    monkeypatch.setattr(hook, "refresh_snapshot", boom)
    return calls


@pytest.mark.parametrize("policy", [
    {},                                                    # no policy at all
    {"paths": {}},                                         # no rules
    {"paths": {"/somewhere/else": {"paced": True}}},        # rule for another tree
    {"global": {"enabled": False},
     "paths": {"/work": {"paced": True}}},                  # kill switch on
    {"paths": {"/work": {"paced": False}}},                 # explicitly unpaced
])
def test_unpaced_folder_never_refreshes(state_dir, no_refresh_allowed, policy):
    """The cheap gate must come first.

    No snapshot exists here at all, so the pre-fix code would have treated the
    state as stale and immediately shelled out. An unpaced folder needs no usage
    data whatsoever.
    """
    write(state_dir / "policy.json", policy)
    brake_start, why = hook.run(hook.norm_path("/work"))
    assert brake_start is None
    assert why == "unpaced"
    assert no_refresh_allowed == []


def test_unpaced_folder_does_not_even_read_the_snapshot(state_dir, monkeypatch,
                                                       no_refresh_allowed):
    """A stale snapshot on disk must not change an unpaced verdict."""
    write(state_dir / "policy.json", {"paths": {}})
    write(state_dir / "state.json",
          {"ts_epoch": 0, "buckets": {}})          # ancient and empty
    brake_start, why = hook.run(hook.norm_path("/work"))
    assert why == "unpaced"
    assert no_refresh_allowed == []


def test_paced_folder_with_fresh_snapshot_also_avoids_refresh(state_dir,
                                                             no_refresh_allowed):
    """A paced folder under the line, with current data, should not refresh."""
    now = int(time.time())
    write(state_dir / "policy.json",
          {"paths": {"/work": {"paced": True, "model": "opus"}}})
    write(state_dir / "state.json", {
        "ts_epoch": now,
        "buckets": {
            "session": {"pct": 1, "resets_epoch": now + 14400,
                        "window_seconds": 18000, "label": None},
        },
    })
    brake_start, why = hook.run(hook.norm_path("/work"))
    assert brake_start is None
    assert why == "line-caught-up"
    assert no_refresh_allowed == []


def test_paced_folder_with_stale_snapshot_does_refresh(state_dir, monkeypatch):
    """The converse: staleness on a *paced* folder must still trigger a refresh,
    otherwise the self-healing path is dead."""
    calls = []
    monkeypatch.setattr(hook, "refresh_snapshot", lambda: calls.append(1) or False)
    # A failed refresh leaves us blind, which brakes; keep the nap short so the
    # test does not actually sleep for a chunk.
    monkeypatch.setattr(hook.time, "sleep", lambda _s: (_ for _ in ()).throw(
        KeyboardInterrupt()))
    write(state_dir / "policy.json",
          {"paths": {"/work": {"paced": True}}, "defaults": {"chunk": 1}})
    write(state_dir / "state.json", {"ts_epoch": 0, "buckets": {}})
    with pytest.raises(KeyboardInterrupt):
        hook.run(hook.norm_path("/work"))
    assert calls, "a stale snapshot on a paced folder must trigger a refresh"
