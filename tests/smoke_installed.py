#!/usr/bin/env python3
"""Integration smoke test for the INSTALLED entry points.

The pytest suite imports modules directly, so it cannot catch anything about
packaging: whether the console scripts exist, whether `install` finds the hook
executable, whether real filesystem paths normalize correctly on this platform.
That is exactly the class of bug most likely to appear on Windows.

Deliberately requires no Claude Code and no network. All state is redirected to a
temp directory via NICECLAUDE_DIR, so it cannot disturb a real installation.

Run:  python tests/smoke_installed.py
"""

import json
import os
import shutil
import subprocess
import sys
import tempfile
import time

FAILURES = []


def check(name, ok, detail=""):
    print(f"  {'PASS' if ok else 'FAIL'}  {name}" + (f"  — {detail}" if detail else ""))
    if not ok:
        FAILURES.append(name)


def run(args, env, timeout=60):
    return subprocess.run(args, env=env, capture_output=True, text=True,
                          timeout=timeout)


def hook_blocks(hook, cwd, env, seconds=6):
    """True if the hook is still sleeping after `seconds` — i.e. it braked.

    A brake is an unbounded sleep, so the only way to observe it portably is to
    wait a little and see whether the process is still alive. subprocess's
    timeout is the cross-platform stand-in for `timeout(1)`.
    """
    payload = json.dumps({"cwd": cwd, "hook_event_name": "PreToolUse"})
    try:
        proc = subprocess.run([hook], input=payload, capture_output=True,
                              text=True, timeout=seconds, env=env)
        return False, proc.returncode
    except subprocess.TimeoutExpired:
        return True, None


def main():
    tmp = tempfile.mkdtemp(prefix="niceclaude-smoke-")
    env = dict(os.environ, NICECLAUDE_DIR=os.path.join(tmp, "state"))
    work = os.path.join(tmp, "work")
    other = os.path.join(tmp, "workXTRA")   # must NOT match a rule for `work`
    os.makedirs(work)
    os.makedirs(other)

    nc = shutil.which("niceclaude")
    hook = shutil.which("niceclaude-hook")
    print(f"niceclaude:      {nc}")
    print(f"niceclaude-hook: {hook}")
    print(f"state dir:       {env['NICECLAUDE_DIR']}")
    print(f"platform:        {sys.platform}\n")

    check("both console scripts on PATH", bool(nc) and bool(hook))
    if not (nc and hook):
        return 1

    r = run([nc, "install"], env)
    check("niceclaude install succeeds", r.returncode == 0,
          (r.stderr or r.stdout).strip().splitlines()[-1] if r.returncode else "")

    # The settings fragment must exist and point at a command that resolves.
    settings = None
    for line in r.stdout.splitlines():
        if line.startswith("settings:"):
            settings = line.split(":", 1)[1].strip()
    check("install reports a settings path", bool(settings), settings or "")
    if settings and os.path.exists(settings):
        with open(settings, encoding="utf-8") as fh:
            cfg = json.load(fh)
        cmd = cfg["hooks"]["PreToolUse"][0]["hooks"][0]["command"]
        bare = cmd.strip('"')
        check("hook command in settings resolves to a real file",
              os.path.exists(bare), cmd)
        check("hook command is quoted when the path contains a space",
              (" " not in bare) or cmd.startswith('"'), cmd)
    else:
        check("settings file written", False, settings or "")

    # An unpaced folder must exit 0, and fast.
    t0 = time.monotonic()
    blocked, rc = hook_blocks(hook, work, env, seconds=20)
    elapsed = (time.monotonic() - t0) * 1000
    check("hook allows an unpaced folder", (not blocked) and rc == 0,
          f"{elapsed:.0f}ms")
    check("hot path is not pathologically slow", elapsed < 2000, f"{elapsed:.0f}ms")

    # Pace the folder and publish a snapshot that is comfortably under the line.
    r = run([nc, "on", work, "--model", "opus"], env)
    check("niceclaude on succeeds", r.returncode == 0, r.stderr.strip())

    now = int(time.time())
    state = {
        "ts_epoch": now, "ts": "smoke", "ok": True,
        "buckets": {
            "session": {"pct": 1, "resets_epoch": now + 14400,
                        "window_seconds": 18000, "label": None},
            "week:all models": {"pct": 1, "resets_epoch": now + 500000,
                                "window_seconds": 604800, "label": "all models"},
        },
    }
    state_path = os.path.join(env["NICECLAUDE_DIR"], "state.json")
    os.makedirs(os.path.dirname(state_path), exist_ok=True)
    with open(state_path, "w", encoding="utf-8") as fh:
        json.dump(state, fh)

    blocked, rc = hook_blocks(hook, work, env, seconds=20)
    check("paced folder under the line is allowed", (not blocked) and rc == 0)

    # Path handling: a sibling whose name merely starts with the paced path's
    # name must not match. This is the silent-mispacing bug.
    blocked, rc = hook_blocks(hook, other, env, seconds=20)
    check("sibling prefix does not match the rule", (not blocked) and rc == 0,
          os.path.basename(other))

    # Case-insensitive match, which is what normcase exists for on Windows.
    if sys.platform.startswith("win"):
        r = run([nc, "status", work.upper()], env)
        check("status matches a case-different path (Windows)",
              "paced         True" in r.stdout, r.stdout.splitlines()[2:4])

    # Now push usage above the line and confirm it actually brakes.
    state["buckets"]["session"]["pct"] = 90
    with open(state_path, "w", encoding="utf-8") as fh:
        json.dump(state, fh)
    blocked, rc = hook_blocks(hook, work, env, seconds=8)
    check("paced folder over the line BRAKES", blocked, "still sleeping at 8s")

    # And that the kill switch releases it.
    r = run([nc, "global", "off"], env)
    blocked, rc = hook_blocks(hook, work, env, seconds=20)
    check("global off releases a braked folder", (not blocked) and rc == 0)
    run([nc, "global", "on"], env)

    shutil.rmtree(tmp, ignore_errors=True)
    print()
    if FAILURES:
        print(f"{len(FAILURES)} FAILED: {', '.join(FAILURES)}")
        return 1
    print("all installed-entry-point checks passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
