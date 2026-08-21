#!/usr/bin/env python3
"""Integration smoke test for the INSTALLED entry points.

The pytest suite imports modules directly, so it cannot catch anything about
packaging: whether the console scripts exist, whether `install` finds the hook
executable, whether real filesystem paths normalize correctly on this platform.
That is exactly the class of bug most likely to appear on Windows.

Deliberately requires no Claude Code and no network. All state is redirected to a
temp directory via NICECLAUDE_DIR, and CLAUDE_CONFIG_DIR is redirected too --
`install` edits Claude Code's own settings file, so without that this would
rewrite the real ~/.claude/settings.json of whoever runs it.

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
    env = dict(os.environ,
               NICECLAUDE_DIR=os.path.join(tmp, "state"),
               CLAUDE_CONFIG_DIR=os.path.join(tmp, "claude"))
    env.pop("NICECLAUDE_OFF", None)   # an exemption in the caller's shell would
                                      # make every brake assertion below pass
                                      # for the wrong reason
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

    # The version is read from distribution metadata rather than a literal in
    # the source, so the lookup can only fail in a real install -- a tool venv
    # where dist-info is not where importlib.metadata looks. That is precisely
    # what this file, and nothing in the pytest suite, exercises.
    sub = run([nc, "version"], env)
    flag = run([nc, "--version"], env)
    check("niceclaude version prints a version", sub.returncode == 0
          and sub.stdout.startswith("niceclaude "),
          sub.stdout.strip() or (sub.stderr or "").strip())
    check("--version agrees with the subcommand",
          flag.returncode == 0 and flag.stdout == sub.stdout,
          f"{flag.stdout.strip()!r} vs {sub.stdout.strip()!r}")

    # Seed Claude Code's settings with something to protect, so the merge is
    # exercised against real files rather than into a vacuum.
    claude_settings = os.path.join(env["CLAUDE_CONFIG_DIR"], "settings.json")
    os.makedirs(env["CLAUDE_CONFIG_DIR"])
    with open(claude_settings, "w", encoding="utf-8") as fh:
        json.dump({"model": "sonnet", "hooks": {"Stop": [
            {"matcher": "*", "hooks": [
                {"type": "command", "command": "echo done"}]}]}}, fh)

    r = run([nc, "install"], env)
    check("niceclaude install succeeds", r.returncode == 0,
          (r.stderr or r.stdout).strip().splitlines()[-1] if r.returncode else "")

    def hook_commands(path, event="PreToolUse"):
        with open(path, encoding="utf-8") as fh:
            cfg = json.load(fh)
        return [e.get("command") for g in cfg.get("hooks", {}).get(event, [])
                for e in g.get("hooks", [])], cfg

    check("install wrote into Claude Code's settings",
          os.path.exists(claude_settings), claude_settings)
    cmds, cfg = hook_commands(claude_settings)
    ours = [c for c in cmds if "niceclaude-hook" in c]
    check("the hook is registered on PreToolUse", len(ours) == 1, str(cmds))
    check("unrelated settings survived the merge",
          cfg.get("model") == "sonnet"
          and cfg["hooks"]["Stop"][0]["hooks"][0]["command"] == "echo done")

    if ours:
        bare = ours[0].strip('"')
        check("registered command resolves to a real file",
              os.path.exists(bare), ours[0])
        check("command is quoted when the path contains a space",
              (" " not in bare) or ours[0].startswith('"'), ours[0])

    # Reinstalling must update in place. A duplicate would double the per-call
    # latency, and after the tool venv moves one of the two would be dead.
    run([nc, "install"], env)
    cmds2, _ = hook_commands(claude_settings)
    check("a second install does not duplicate the hook", cmds2 == cmds, str(cmds2))

    # The --settings fragment is still written, for the hook-free foreground case.
    fragment = None
    for line in r.stdout.splitlines():
        if line.startswith("fragment:"):
            fragment = line.split(":", 1)[1].split("  (")[0].strip()
    check("install reports a fragment path and it exists",
          bool(fragment) and os.path.exists(fragment), fragment or "")

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
              "paced         True" in r.stdout, r.stdout.strip())

    # Now push usage above the line and confirm it actually brakes.
    state["buckets"]["session"]["pct"] = 90
    with open(state_path, "w", encoding="utf-8") as fh:
        json.dump(state, fh)
    blocked, rc = hook_blocks(hook, work, env, seconds=8)
    check("paced folder over the line BRAKES", blocked, "still sleeping at 8s")

    # NICECLAUDE_OFF must beat a policy that is definitely braking -- the check
    # above establishes that it is. This is the only exemption that works
    # per-session, so it is the one holding up the global install.
    exempt = dict(env, NICECLAUDE_OFF="1")
    t0 = time.monotonic()
    blocked, rc = hook_blocks(hook, work, exempt, seconds=8)
    check("NICECLAUDE_OFF exempts a session that would otherwise brake",
          (not blocked) and rc == 0, f"{(time.monotonic() - t0) * 1000:.0f}ms")
    r = run([nc, "status", work], exempt)
    check("status reports the exemption rather than claiming it is paced",
          "EXEMPT" in r.stdout, r.stdout.splitlines()[1] if r.stdout else "")

    # And that the kill switch releases it.
    r = run([nc, "global", "off"], env)
    blocked, rc = hook_blocks(hook, work, env, seconds=20)
    check("global off releases a braked folder", (not blocked) and rc == 0)
    run([nc, "global", "on"], env)

    # uninstall backs the merge out and leaves the rest of the file intact.
    r = run([nc, "uninstall"], env)
    check("niceclaude uninstall succeeds", r.returncode == 0, r.stderr.strip())
    cmds3, cfg3 = hook_commands(claude_settings)
    check("uninstall removed our hook", not any("niceclaude-hook" in c for c in cmds3),
          str(cmds3))
    check("uninstall left the rest of the settings alone",
          cfg3.get("model") == "sonnet"
          and cfg3["hooks"]["Stop"][0]["hooks"][0]["command"] == "echo done")
    check("uninstall kept the policy",
          os.path.exists(os.path.join(env["NICECLAUDE_DIR"], "policy.json")))

    shutil.rmtree(tmp, ignore_errors=True)
    print()
    if FAILURES:
        print(f"{len(FAILURES)} FAILED: {', '.join(FAILURES)}")
        return 1
    print("all installed-entry-point checks passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
