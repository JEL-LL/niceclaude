"""niceclaude -- pace Claude Code background work against its own usage windows.

The control law
---------------
Each usage window has a start, an end, and a percentage consumed. If we are
f_t of the way through the window's *time*, we should be at most f_t of the
way through its *budget*. That diagonal is the pace line:

    allowed(f_t) = m0 + f_t * (100 - m0 - m1)

m0 is a starting grubstake, without which nothing could ever begin (the pure
diagonal permits 0% at 0% elapsed). m1 is an end-of-window reserve, so we come
in under the wire rather than exactly on it.

Above the line means braking until the line rises to meet us. Usage never
falls, so the wake time is solvable in closed form -- but it is not a
commitment: the foreground session and sibling agents draw on the same
account-global budget, so it is recomputed from fresh data on every poll and
can move later while we wait.

Quantization
------------
`/usage` reports whole percentages. That 1% quantum floors how finely we can
act: 1% of the 5h session window is 3 minutes, 1% of the weekly window is 100
minutes. The quantum therefore supplies a deadband for free, which is why none
is configured. It also means a reported P% could be anything up to (P+1)%, so
the hook rounds against itself.

Architecture
------------
This process is tier 2: it polls, logs, and publishes a raw usage snapshot to
state.json. It does not decide. Decisions are per-folder, and the hot path
(hook.py, reached via the niceclaude-hook entry point) computes them itself from
the snapshot -- so one snapshot serves many folders under different policies.
"""

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import time
from datetime import datetime, timedelta, timezone

from . import hook
from ._shared import (  # noqa: E402
    CONFIG_DIR, DATA_DIR, DEFAULT_CHUNK, DEFAULT_FANOUT_RESERVE, DEFAULT_M0,
    DEFAULT_M1, DEFAULT_MAX_DELAY, DEFAULT_POLICY, HOME,
    LOG_PATH, POLICY_PATH, SETTINGS_PATH,
    MAX_STALE, STATE_PATH, WINDOW_SECONDS, bucket_pace, model_matches,
    norm_path, normalize_enforce, path_within,
)

# "Current session: 11% used · resets Aug 14, 8:10pm (UTC)"
# "Current week (all models): 14% used · resets Aug 16, 12am (UTC)"
# "Current session: 0% used"                 <- seen right after a window rolls
#
# The reset clause is OPTIONAL: immediately after a window resets, the server
# emits the bucket with no reset time at all. Requiring it turned a perfectly
# healthy 0%-used window into an unparsed line.
# Separator is U+00B7; tolerate ASCII variants in case the rendering changes.
#
# In the pattern below that separator is written as a regex escape, NOT as the
# character itself, and must stay that way. A literal is a live hazard here:
# anything that reads this file as cp1252 and writes it back as UTF-8 -- a
# PowerShell `Get-Content | Set-Content` round-trip does exactly that --
# double-encodes it silently, and the pattern then stops matching real
# `/usage` output in precisely the way the encoding bug already did once.
# The escape keeps the pattern pure ASCII, so no such round-trip can touch it.
# tests/test_source_encoding.py enforces this.
LINE_RE = re.compile(
    r"^Current\s+(?P<window>session|week)"
    r"(?:\s*\((?P<label>[^)]*)\))?"
    r"\s*:\s*"
    r"(?P<pct>[<>]?\s*[\d.]+)\s*%\s*used"
    # The timezone is whatever the renderer prints in parentheses. [A-Z]{2,5}
    # only ever matched a box set to UTC; a normal workstation reports an IANA
    # name ("America/New_York"), which failed the class, failed the optional
    # group, and so failed the whole line -- losing the session and
    # week:all-models buckets entirely. Accept anything up to the closing
    # paren, the same way the bucket label above already does.
    r"(?:\s*[\u00b7*|-]\s*resets\s+(?P<resets>.+?)"
    r"\s*\((?P<tz>[^)]+)\))?"
    r"\s*$",
    re.IGNORECASE,
)

# `/usage` also prints an advisory block ("What's contributing to your limits
# usage?", request counts, top subagents) that appears only once there is
# something to report. It is not bucket data and must not be treated as a parse
# failure -- but neither can every unrecognised line be waved through, or the
# over-limit rendering we have never seen would be silently ignored. So: lines
# starting with "Current" MUST parse, and anything carrying limit-ish vocabulary
# is surfaced loudly. Everything else is advisory.
SUSPICIOUS = ("resets", "limit reached", "rate limit", "exceeded",
              "unavailable", "try again", "out of")

# Preambles the renderer is known to emit. The overage variant mentions "rate
# limits", which would otherwise trip the SUSPICIOUS check and report a false
# anomaly for the entire time an account is on overage billing.
KNOWN_PREAMBLES = ("you are currently using your",)


def classify(line):
    low = line.lower()
    if low.startswith("current "):
        return "bucket"
    if any(low.startswith(p) for p in KNOWN_PREAMBLES):
        return "info"
    return "suspicious" if any(s in low for s in SUSPICIOUS) else "info"

RESET_JITTER = 120  # the server rounds; 8:09pm and 8:10pm are the same instant

# Reported usage is very nearly monotonic within a window, but not exactly.
# The renderer floors a float, so a small backend recalculation that crosses
# an integer boundary shows up as a 1-point drop (4.02 -> 3.98 renders 4 -> 3).
# Measured once in 4015 session samples, never in a weekly bucket. Flagging it
# would make `check` cry wolf over the only anomaly in the whole corpus, and a
# checker that is routinely wrong stops being read. Larger drops are still
# reported: those cannot be rounding.
PCT_JITTER = 1

HOOK_EVENTS = ["PreToolUse", "SubagentStart"]


def utcnow():
    return datetime.now(timezone.utc)


# --- parsing -----------------------------------------------------------------

def resolve_tz(label):
    """tzinfo for a `/usage` timezone label, or None if it cannot be resolved.

    On a machine set to UTC the renderer prints "UTC"; on an ordinary
    workstation it prints an IANA name -- "America/New_York" -- and the time
    beside it is local, not UTC.

    zoneinfo resolves IANA names wherever a tz database exists, which covers
    Linux and macOS. Windows ships none, so `tzdata` is declared as a dependency
    there (see pyproject.toml) and resolution works on every platform.

    The None fallback below is therefore not the normal path any more, only a
    backstop for an installation missing tzdata (`--no-deps`, a vendored copy).
    It reads the time as machine-local, which is right if the renderer prints
    the machine's own zone -- an inference that held on both machines observed,
    but is not a documented contract. That inference is exactly what declaring
    tzdata buys out, because when it is wrong the error is a whole UTC offset
    and its direction depends on the sign.
    """
    if not label:
        return None
    key = label.strip()
    if key.upper() in ("UTC", "GMT", "Z"):
        return timezone.utc
    try:
        from zoneinfo import ZoneInfo
        return ZoneInfo(key)
    except Exception:
        return None


def parse_reset(text, now, tz_label=None):
    """'Aug 14, 8:10pm' -> aware UTC datetime, or None. Never raises.

    The year is absent from the output. Supply it explicitly rather than
    patching it in afterwards: year-less strptime is deprecated in 3.14+ and
    mishandles Feb 29. Reset times are always ahead of now, so try this year
    then next and keep whichever lands in the future.

    The time is expressed in the zone named beside it, NOT in UTC. Stamping it
    UTC was correct only because the machine this was written on was set to
    UTC. On a workstation in America/New_York it placed every reset four hours
    early, which inflates the elapsed fraction f_t, which raises the pace line,
    which permits spending that should have been braked -- an error in the
    fail-OPEN direction, the one direction this tool must never fail in.
    """
    text = text.strip().rstrip(".")
    tz = resolve_tz(tz_label) if tz_label else timezone.utc
    for fmt in ("%b %d, %I:%M%p", "%b %d, %I%p", "%b %d %I:%M%p", "%b %d %I%p"):
        for year in (now.year, now.year + 1):
            try:
                dt = datetime.strptime(f"{year} {text}", f"%Y {fmt}")
            except ValueError:
                continue
            if tz is not None:
                dt = dt.replace(tzinfo=tz).astimezone(timezone.utc)
            else:
                # Label named a zone we cannot resolve, so read it as local
                # time. astimezone() on a naive datetime assumes the system
                # zone and applies that zone's DST rules for the date in
                # question, which is exactly right when the label is the
                # machine's own zone -- which is what the renderer emits.
                dt = dt.astimezone(timezone.utc)
            if dt >= now - timedelta(days=1):  # a day of slop
                return dt
    return None


def parse_pct(text):
    text = text.strip()
    approx = text[:1] in "<>"
    if approx:
        text = text[1:].strip()
    try:
        val = float(text)
    except ValueError:
        return None, approx
    if approx and val > 0:
        val = val / 2.0
    return val, approx


def parse_usage(raw, now):
    """Return (buckets, unparsed_lines).

    Keyed on the bucket label, never on position: per-model weekly lines appear
    only for models actually used, so the line count varies between samples.
    """
    buckets, unparsed, info = {}, [], []
    for line in raw.splitlines():
        line = line.strip()
        if not line:
            continue
        m = LINE_RE.match(line)
        if not m:
            kind = classify(line)
            if kind == "info":
                info.append(line)
            else:
                unparsed.append(line)
            continue
        window = m.group("window").lower()
        label = (m.group("label") or "").strip()
        key = f"{window}:{label}" if label else window
        pct, approx = parse_pct(m.group("pct"))
        resets_raw = m.group("resets")
        resets = parse_reset(resets_raw, now, m.group("tz")) if resets_raw else None
        buckets[key] = {
            "window": window,
            "label": label or None,
            "pct": pct,
            "pct_approx": approx,
            "resets_raw": resets_raw.strip() if resets_raw else None,
            "resets_epoch": int(resets.timestamp()) if resets else None,
            "tz": m.group("tz"),
            "window_seconds": WINDOW_SECONDS.get(window),
            "line": line,
        }
    return buckets, unparsed, info


def sample_once():
    now = utcnow()
    started = time.monotonic()
    try:
        # encoding is explicit: text=True alone decodes with locale.getencoding(),
        # which on Windows is the ANSI code page (cp1252 on a US install), not
        # UTF-8. `/usage` separates its fields with U+00B7, emitted as the UTF-8
        # bytes C2 B7 -- read as cp1252 those become "Â·", LINE_RE stops
        # matching, and the session and week:all-models buckets vanish from the
        # snapshot while week:Fable (which carries no separator) still parses.
        # The hook then finds no enforceable bucket and brakes blind forever.
        # errors="replace" keeps a partial read surfacing as an unparsed line
        # rather than an exception that would discard the whole sample.
        proc = subprocess.run(["claude", "-p", "/usage"],
                              capture_output=True, text=True, timeout=120,
                              encoding="utf-8", errors="replace")
        raw, err, rc = proc.stdout, proc.stderr, proc.returncode
    except Exception as exc:
        raw, err, rc = "", f"{type(exc).__name__}: {exc}", -1
    elapsed_ms = int((time.monotonic() - started) * 1000)
    buckets, unparsed, info = parse_usage(raw, now)
    return {
        "ts": now.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "ts_epoch": int(now.timestamp()),
        "elapsed_ms": elapsed_ms,
        "exit_code": rc,
        "raw": raw,                       # verbatim: the parser stays re-runnable
        "stderr": err.strip() or None,
        "buckets": buckets,
        "unparsed_lines": unparsed,
        "info_lines": info,
        "parse_ok": bool(buckets) and not unparsed and rc == 0,
    }


# --- io ----------------------------------------------------------------------

def write_atomic(path, text):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = f"{path}.tmp.{os.getpid()}"
    with open(tmp, "w", encoding="utf-8") as fh:
        fh.write(text)
        fh.flush()
        os.fsync(fh.fileno())
    os.replace(tmp, path)  # atomic: the hook never sees a half-written file


def append_log(record):
    os.makedirs(os.path.dirname(LOG_PATH), exist_ok=True)
    with open(LOG_PATH, "a", encoding="utf-8") as fh:
        fh.write(json.dumps(record, ensure_ascii=False) + "\n")
        fh.flush()
        os.fsync(fh.fileno())


def publish_state(rec):
    """Publish the snapshot the hook reads.

    Deliberately carries no decision: those depend on per-folder policy, and
    computing them here would mean one state file per folder.
    """
    write_atomic(STATE_PATH, json.dumps({
        "ts_epoch": rec["ts_epoch"],
        "ts": rec["ts"],
        "ok": rec["exit_code"] == 0 and bool(rec["buckets"]),
        "buckets": {
            k: {"pct": b["pct"], "resets_epoch": b["resets_epoch"],
                "window_seconds": b["window_seconds"], "label": b["label"]}
            for k, b in rec["buckets"].items()
        },
    }, indent=2))


def load_policy():
    if not os.path.exists(POLICY_PATH):
        return json.loads(json.dumps(DEFAULT_POLICY))
    try:
        with open(POLICY_PATH, encoding="utf-8") as fh:
            pol = json.load(fh)
    except (json.JSONDecodeError, OSError):
        return json.loads(json.dumps(DEFAULT_POLICY))
    for k, v in DEFAULT_POLICY.items():
        pol.setdefault(k, v)
    return pol


def save_policy(pol):
    write_atomic(POLICY_PATH, json.dumps(pol, indent=2))


def resolve(pol, path):
    """Longest matching path prefix wins, so subfolders inherit and a deeper
    rule can override a shallower one.

    Matching is on path components, not raw string prefix -- otherwise
    /foo/bar would match /foo/barbaz and silently pace the wrong tree. A rule
    on the filesystem root is a legitimate catch-all and matches everything
    below it.
    """
    target = norm_path(path)
    best = None
    for raw_key in pol.get("paths", {}):
        key = norm_path(raw_key)
        if path_within(target, key):
            if best is None or len(key) > len(best[0]):
                best = (key, raw_key)
    if best is None:
        return None, None
    return best[1], pol["paths"][best[1]]


# --- assertions --------------------------------------------------------------

def load_log():
    if not os.path.exists(LOG_PATH):
        return []
    out = []
    with open(LOG_PATH, encoding="utf-8") as fh:
        for n, line in enumerate(fh, 1):
            line = line.strip()
            if not line:
                continue
            try:
                out.append(json.loads(line))
            except json.JSONDecodeError:
                print(f"  corrupt JSON at log line {n}", file=sys.stderr)
    return out


def cmd_check():
    """Misparse detector. Runs over the whole history, so a parser fix can be
    re-validated against every sample ever taken."""
    records = load_log()
    if not records:
        print("no records")
        return 1
    problems, prev = [], {}
    for rec in records:
        ts = rec["ts"]
        if rec["exit_code"] != 0:
            problems.append(f"{ts}  nonzero exit {rec['exit_code']}: {rec['stderr']}")
            continue
        # Re-parse from the stored raw with the CURRENT parser rather than
        # trusting what was parsed at capture time. That makes this a
        # regression test of today's parser against every sample ever taken.
        when = datetime.fromtimestamp(rec["ts_epoch"], timezone.utc)
        buckets, unparsed, _info = parse_usage(rec["raw"], when)
        for line in unparsed:
            problems.append(f"{ts}  UNPARSED LINE: {line!r}")
        if not buckets:
            problems.append(f"{ts}  no buckets from raw: {rec['raw']!r}")
            continue
        rec["buckets"] = buckets  # so the bucket-key survey below sees reality
        for key, b in buckets.items():
            if b["pct"] is None:
                problems.append(f"{ts}  {key}: unparseable percent")
            # A missing reset clause is legitimate just after a roll; only a
            # clause that was present and unreadable is a problem.
            if b["resets_raw"] is not None and b["resets_epoch"] is None:
                problems.append(f"{ts}  {key}: unparseable reset {b['resets_raw']!r}")
            if key in prev:
                ppct, preset, pts = prev[key]   # ppct is the window's running max
                rolled = (
                    (b["resets_epoch"] is not None and preset is not None
                     and abs(b["resets_epoch"] - preset) > RESET_JITTER)
                    or b["resets_epoch"] is None   # post-roll, no clause yet
                    or b["pct"] == 0               # usage is only 0 at a window start
                )
                # Usage cannot fall inside a window. If it did and the window
                # did not roll, the parser is wrong.
                if (not rolled and None not in (b["pct"], ppct)
                        and b["pct"] < ppct - PCT_JITTER):
                    problems.append(
                        f"{ts}  {key}: usage DECREASED {ppct}% -> {b['pct']}% with no "
                        f"window roll (peak seen {pts}) -- likely misparse")
                # Compare against the window's running MAX, not the previous
                # sample. Against the previous sample a run of 1-point drops is
                # tolerated indefinitely, so a steady slide -- which rounding
                # cannot produce -- would slip through one point at a time.
                if b["pct"] is None:
                    peak, when = ppct, pts
                elif rolled or ppct is None or b["pct"] >= ppct:
                    peak, when = b["pct"], ts
                else:
                    peak, when = ppct, pts
                prev[key] = (peak, b["resets_epoch"], when)
            else:
                prev[key] = (b["pct"], b["resets_epoch"], ts)

    all_keys = set().union(*(set(r.get("buckets", {})) for r in records))
    late = all_keys - set(records[0].get("buckets", {}))
    if late:
        print(f"note: buckets appeared after the first sample: {sorted(late)}")
    span_h = (records[-1]["ts_epoch"] - records[0]["ts_epoch"]) / 3600
    print(f"{len(records)} samples over {span_h:.1f}h; buckets: {sorted(all_keys)}")
    if problems:
        print(f"\n{len(problems)} PROBLEM(S):")
        for p in problems:
            print(f"  {p}")
        return 1
    print("no anomalies")
    return 0


# --- install -----------------------------------------------------------------

def find_hook_exe():
    """Locate the niceclaude-hook console script.

    Installers put it beside the main entry point, so look there first: PATH
    lookup can miss it when the tool venv's bin directory is not on the PATH of
    whatever shell Claude Code uses to run hooks.
    """
    found = shutil.which("niceclaude-hook")
    if found:
        return os.path.abspath(found)
    here = os.path.dirname(os.path.abspath(sys.argv[0] or ""))
    for name in ("niceclaude-hook.exe", "niceclaude-hook"):
        cand = os.path.join(here, name)
        if os.path.exists(cand):
            return cand
    return None


def hook_command():
    """The shell command string to register, or None if the hook is missing.

    Hook commands are run through a shell, so a path containing spaces
    (C:\\Users\\...\\Program Files\\...) has to be quoted or it parses as two
    arguments and the hook silently never fires.

    On Windows that shell is a POSIX one -- Claude Code runs hooks through Git
    Bash, verified by a hook command of `echo x > /c/tmp/marker` landing at
    C:\\tmp\\marker. There, backslash is an ESCAPE character, so a native path
    like C:\\Users\\me\\.local\\bin\\niceclaude-hook.exe loses every separator
    and becomes C:Usersme.localbinniceclaude-hook.exe, no such file exists, and
    the hook never runs. Nothing reports this: the folder looks paced while
    being completely ungoverned, the worst failure mode this tool has.

    Forward slashes avoid the escaping entirely and are accepted by the Windows
    API, so the command works whether the shell is sh or cmd.exe.
    """
    hook_exe = find_hook_exe()
    if not hook_exe:
        return None
    if os.name == "nt":
        hook_exe = hook_exe.replace("\\", "/")
    return f'"{hook_exe}"' if " " in hook_exe else hook_exe


def claude_settings_path():
    """Claude Code's user settings file -- the file `install` writes into.

    Honours CLAUDE_CONFIG_DIR, which is how Claude Code itself relocates the
    directory. Resolved on each call rather than at import, because the test
    suite must be able to redirect it and because a daemon outliving an
    environment change should not keep writing to a stale path.
    """
    base = os.environ.get("CLAUDE_CONFIG_DIR") or os.path.join(HOME, ".claude")
    return os.path.join(base, "settings.json")


def is_our_hook(entry):
    """Is this hook entry one of ours?

    Matched on the executable's basename rather than the full command, so a
    reinstall after the tool venv moves *updates* the existing entry instead of
    appending a second one. Two registrations would double the per-call latency;
    worse, after a path change one of them is dead and the file looks correct.
    """
    if not isinstance(entry, dict):
        return False
    cmd = entry.get("command")
    if not isinstance(cmd, str):
        return False
    return os.path.basename(cmd.strip().strip('"')).startswith("niceclaude-hook")


def load_settings(path):
    """Read a settings file for editing. Returns (dict, error-message).

    A file we cannot parse is never overwritten -- it is someone's real
    configuration, and clobbering `model`, `permissions` or their own hooks to
    install ours would be a far worse bug than refusing to install.
    """
    if not os.path.exists(path):
        return {}, None
    try:
        with open(path, encoding="utf-8") as fh:
            cfg = json.load(fh)
    except (OSError, ValueError) as exc:
        return None, f"could not read {path}: {exc}"
    if not isinstance(cfg, dict):
        return None, f"{path} does not contain a JSON object"
    return cfg, None


def splice_hook(cfg, command):
    """Register `command` on every hook event in `cfg`, in place.

    Creates only the structure that is missing and touches only the keys it
    owns: other top-level settings, other events, other people's hooks on the
    same event, and a matcher the user has deliberately narrowed all survive.

    Returns (changed, error-message).
    """
    hooks = cfg.setdefault("hooks", {})
    if not isinstance(hooks, dict):
        return False, 'the "hooks" key is not an object'

    changed = False
    for event in HOOK_EVENTS:
        groups = hooks.setdefault(event, [])
        if not isinstance(groups, list):
            return False, f'hooks.{event} is not an array'

        existing = None
        for group in groups:
            if not isinstance(group, dict):
                continue
            for entry in group.get("hooks") or []:
                if is_our_hook(entry):
                    existing = entry
                    break
            if existing:
                break

        if existing is None:
            groups.append({"matcher": "*", "hooks": [
                {"type": "command", "command": command, "timeout": 21600}]})
            changed = True
        elif existing.get("command") != command:
            existing["command"] = command
            existing["timeout"] = 21600
            changed = True

    return changed, None


def unsplice_hook(cfg):
    """Remove our hook entries from `cfg`, in place. Returns True if changed.

    Prunes the containers it empties: a matcher group with no hooks left, an
    event with no groups left, and `hooks` itself. It cannot tell a container it
    emptied from one that was already empty, so pruning is decided on emptiness
    -- but only for events we actually removed from, so an empty event the user
    put there themselves survives. Nothing is lost either way: an empty hook
    list and an absent key mean the same thing to Claude Code.
    """
    hooks = cfg.get("hooks")
    if not isinstance(hooks, dict):
        return False

    changed = False
    for event in list(hooks):
        groups = hooks.get(event)
        if not isinstance(groups, list):
            continue
        removed_here = False
        for group in list(groups):
            if not isinstance(group, dict) or not isinstance(group.get("hooks"), list):
                continue
            kept = [e for e in group["hooks"] if not is_our_hook(e)]
            if len(kept) == len(group["hooks"]):
                continue
            removed_here = changed = True
            if kept:
                group["hooks"] = kept
            else:
                groups.remove(group)
        # Only prune what we emptied. Tracked per event, not with `changed`:
        # an event the user left as an empty list must survive a removal that
        # happened under some *other* event.
        if removed_here and not groups:
            del hooks[event]
    if changed and not hooks:
        del cfg["hooks"]
    return changed


def registered_in(path):
    """Whether our hook is registered in `path`, for reporting."""
    cfg, err = load_settings(path)
    if err or not cfg:
        return False
    hooks = cfg.get("hooks")
    if not isinstance(hooks, dict):
        return False
    return any(is_our_hook(entry)
               for groups in hooks.values() if isinstance(groups, list)
               for group in groups if isinstance(group, dict)
               for entry in (group.get("hooks") or []))


def cmd_install(force):
    """Register the hook in Claude Code's user settings.

    This writes into ~/.claude/settings.json, so `niceclaude on <folder>` is all
    it takes afterwards -- no special launch. That is a reversal of the original
    design, which wrote only a --settings fragment on the grounds that hooks
    merge additively across scopes and a narrower scope cannot un-register a
    broader one, so a global install could not be exempted.

    The constraint is real; the conclusion was not. The hook already answers
    "is this folder paced?" from policy.json alone -- no snapshot, no
    subprocess -- so it is a genuine no-op everywhere no rule matches. And the
    one gap settings scope cannot close (two sessions in the *same* folder, one
    paced and one not) is closed by NICECLAUDE_OFF instead, which is per-process
    and therefore finer-grained than any settings file.

    The fragment is still written, for anyone who wants foreground sessions to
    be not merely exempt but hook-free.
    """
    command = hook_command()
    if not command:
        print("error: could not find the niceclaude-hook executable. Install the "
              "package first (uv tool install niceclaude).", file=sys.stderr)
        return 1

    os.makedirs(DATA_DIR, exist_ok=True)
    os.makedirs(CONFIG_DIR, exist_ok=True)

    if not os.path.exists(POLICY_PATH) or force:
        save_policy(json.loads(json.dumps(DEFAULT_POLICY)))

    fragment = {"hooks": {
        ev: [{"matcher": "*", "hooks": [
            {"type": "command", "command": command, "timeout": 21600}]}]
        for ev in HOOK_EVENTS
    }}
    write_atomic(SETTINGS_PATH, json.dumps(fragment, indent=2))

    target = claude_settings_path()
    cfg, err = load_settings(target)
    if err:
        print(f"error: {err}\n"
              f"       Refusing to overwrite it. Fix or move the file and rerun,\n"
              f"       or launch with: claude --settings {SETTINGS_PATH}",
              file=sys.stderr)
        return 1

    changed, err = splice_hook(cfg, command)
    if err:
        print(f"error: {target}: {err}\n"
              f"       Refusing to rewrite a shape we do not understand.",
              file=sys.stderr)
        return 1
    if changed:
        write_atomic(target, json.dumps(cfg, indent=2) + "\n")

    print(f"hook:     {command}")
    print(f"settings: {target}"
          f"{'' if changed else '  (already registered)'}")
    print(f"fragment: {SETTINGS_PATH}  (optional, for --settings)")
    print(f"policy:   {POLICY_PATH}")
    print("\nnext:")
    print("  niceclaude on <folder> --model opus")
    print("  niceclaude watch")
    print("\nEvery session now consults the policy; folders with no rule are\n"
          "untouched. NICECLAUDE_OFF=1 exempts a single session.")
    return 0


def cmd_uninstall():
    """Un-register the hook, leaving everything else in the file intact.

    The counterpart to a merging install: once `install` edits a file it does
    not own, there has to be a way back out that does not involve hand-editing
    JSON. Policy and logs are left alone -- this stops pacing, it does not
    forget your configuration.
    """
    target = claude_settings_path()
    cfg, err = load_settings(target)
    if err:
        print(f"error: {err}", file=sys.stderr)
        return 1

    if unsplice_hook(cfg):
        write_atomic(target, json.dumps(cfg, indent=2) + "\n")
        print(f"removed the hook from {target}")
    else:
        print(f"no niceclaude hook registered in {target}")

    if os.path.exists(SETTINGS_PATH):
        os.remove(SETTINGS_PATH)
        print(f"removed the fragment {SETTINGS_PATH}")
    print(f"policy and logs left alone ({DATA_DIR})")
    return 0


# --- commands ----------------------------------------------------------------

PID_PATH = os.path.join(DATA_DIR, "daemon.pid")


def pid_alive(pid):
    """Cross-platform liveness check, without pulling in a dependency."""
    if os.name == "nt":
        import ctypes
        PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
        handle = ctypes.windll.kernel32.OpenProcess(
            PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
        if handle:
            ctypes.windll.kernel32.CloseHandle(handle)
            return True
        return False
    try:
        os.kill(pid, 0)
    except (OSError, ProcessLookupError):
        return False
    return True


def read_pid():
    try:
        with open(PID_PATH, encoding="utf-8") as fh:
            pid = int(fh.read().strip())
    except (OSError, ValueError):
        return None
    return pid if pid_alive(pid) else None


def cmd_stop():
    pid = read_pid()
    if pid is None:
        print("no daemon running")
        return 0
    if os.name == "nt":
        subprocess.run(["taskkill", "/PID", str(pid), "/F"], capture_output=True)
    else:
        import signal
        os.kill(pid, signal.SIGTERM)
    print(f"stopped daemon (pid {pid})")
    return 0


def cmd_watch(interval):
    # A pidfile, rather than matching on the process name. Name matching is
    # treacherous: any shell whose command line merely *mentions* the daemon
    # matches too, so a stop command can kill its own wrapper.
    existing = read_pid()
    if existing:
        print(f"error: daemon already running (pid {existing}); "
              f"`niceclaude stop` first", file=sys.stderr)
        return 1
    os.makedirs(DATA_DIR, exist_ok=True)

    # Python's default SIGTERM handler terminates without unwinding, so the
    # `finally` below would never run and every `stop`/`systemctl stop`/
    # `docker stop` would leave a stale pidfile. read_pid() liveness-checks so
    # that is usually harmless -- but a recycled PID (likely in a fresh
    # container PID namespace with a bind-mounted data dir) would make the next
    # `watch` refuse to start. Raising SystemExit instead lets cleanup happen.
    import signal

    def _terminate(_signum, _frame):
        raise SystemExit(0)

    for sig in ("SIGTERM", "SIGINT", "SIGHUP"):
        handler = getattr(signal, sig, None)
        if handler is not None:
            try:
                signal.signal(handler, _terminate)
            except (ValueError, OSError, AttributeError):
                pass  # not on the main thread, or unsupported on this platform

    write_atomic(PID_PATH, str(os.getpid()))
    try:
        return _watch_loop(interval)
    except (KeyboardInterrupt, SystemExit):
        return 0
    finally:
        try:
            os.remove(PID_PATH)
        except OSError:
            pass


def _watch_loop(interval):
    print(f"niceclaude: polling every {interval}s -> {LOG_PATH} / {STATE_PATH}",
          file=sys.stderr)
    while True:
        rec = sample_once()
        append_log(rec)
        if rec["exit_code"] == 0 and rec["buckets"]:
            publish_state(rec)
        else:
            # Leave the old snapshot alone and let it age out. The hook treats a
            # stale snapshot as unknown and refreshes synchronously rather than
            # trusting it.
            print(f"{rec['ts']} poll failed rc={rec['exit_code']} "
                  f"unparsed={rec['unparsed_lines']}", file=sys.stderr)
        time.sleep(max(1, interval - rec["elapsed_ms"] / 1000))


def cmd_on(path, model, m0, m1, fanout_reserve, enforce, max_delay,
           no_max_delay=False):
    pol = load_policy()
    key = norm_path(path)
    entry = pol["paths"].get(key, {})
    entry["paced"] = True
    if model:
        entry["model"] = model
    if m0 is not None:
        entry["m0"] = m0
    if m1 is not None:
        entry["m1"] = m1
    if fanout_reserve is not None:
        entry["fanout_reserve"] = fanout_reserve
    if enforce is not None:
        entry["enforce"] = sorted(normalize_enforce(enforce))
    if no_max_delay:
        # An explicit null, not a pop. Popping would fall back to a
        # defaults-level cap, so "turn it off" would silently leave one on
        # wherever a default is configured. dict.get only fires its fallback on
        # a MISSING key, so a stored null reads back as "no cap" -- the same
        # missing-key-vs-falsy distinction the pace-line code depends on.
        entry["max_delay"] = None
    elif max_delay is not None:
        entry["max_delay"] = max_delay
    pol["paths"][key] = entry
    save_policy(pol)
    print(f"paced: {key} -> {json.dumps(entry)}")
    if not entry.get("model"):
        print("note: no model declared. The hook cannot discover the running "
              "model, so the per-model weekly bucket will not be enforced.")
    return 0


def cmd_off(path):
    pol = load_policy()
    key = norm_path(path)
    entry = pol["paths"].get(key, {})
    entry["paced"] = False
    pol["paths"][key] = entry
    save_policy(pol)
    print(f"unpaced: {key}")
    return 0


def cmd_global(enabled):
    pol = load_policy()
    pol.setdefault("global", {})["enabled"] = enabled
    save_policy(pol)
    print(f"global.enabled = {enabled}")
    return 0


def describe_installation():
    """One line on whether anything is actually positioned to pace this folder.

    `status` printing `paced True` while no hook is registered anywhere is the
    exact failure this tool most needs to never commit: the folder looks
    governed and every tool call sails through. Policy and plumbing are separate
    facts, so status reports both.
    """
    if os.environ.get("NICECLAUDE_OFF"):
        return ("EXEMPT -- NICECLAUDE_OFF is set in this shell, so a session\n"
                "                started from here is not paced at all")
    target = claude_settings_path()
    if registered_in(target):
        return f"registered in {target}"
    if registered_in(SETTINGS_PATH):
        return (f"fragment only -- reaches only sessions started with\n"
                f"                --settings {SETTINGS_PATH}\n"
                f"                Run `niceclaude install` to pace every session.")
    return ("NOT REGISTERED -- nothing is pacing anything. Run: niceclaude install")


def human_delta(seconds):
    """Compact duration: 5d00h, 4h12m, 12m03s, 42s.

    Rounded up at every scale. A wait that reads as shorter than it is would be
    the one rounding error a person actually notices, because they will sit and
    watch for it. Weekly-line waits run to days, hence the top unit: 119h is a
    number you have to stop and divide.
    """
    s = max(0, int(seconds + 0.999))
    if s >= 86400:
        h = -(-s // 3600)
        return f"{h // 24}d{h % 24:02d}h"
    if s >= 3600:
        m = -(-s // 60)
        return f"{m // 60}h{m % 60:02d}m"
    if s >= 60:
        return f"{s // 60}m{s % 60:02d}s"
    return f"{s}s"


def describe_hold(p, enforced, chunk):
    """How long this one line would hold work, in the row's last column.

    An ignored bucket still gets its wait computed, but phrased as the
    hypothetical it is: "would hold" is a line you could switch on with
    `--enforce`, "HOLDS" is one stopping you now.
    """
    if not p["over"]:
        return "clear"
    if p["wait"] is None:
        # Over the m0 floor with no reset clause to solve against. The hook
        # cannot compute a release, so it just re-checks on the chunk.
        return (f"{'HOLDS' if enforced else 'would hold'} "
                f"-- unsolvable, re-checks every {human_delta(chunk)}")
    return f"{'HOLDS' if enforced else 'would hold'} {human_delta(p['wait'])}"


def cmd_status(path):
    pol = load_policy()
    key = norm_path(path)
    matched, entry = resolve(pol, key)
    genabled = pol.get("global", {}).get("enabled", True)
    print(f"folder:         {key}")
    print(f"hook:           {describe_installation()}")
    print(f"global.enabled: {genabled}")
    if matched is None:
        print("matched rule:   <none>  -> NOT paced")
        return 0
    dflt = pol.get("defaults", {})
    m0 = entry.get("m0", dflt.get('m0', DEFAULT_M0))
    m1 = entry.get("m1", dflt.get('m1', DEFAULT_M1))
    chunk = entry.get("chunk", dflt.get("chunk", DEFAULT_CHUNK))
    max_delay = entry.get("max_delay", dflt.get("max_delay", DEFAULT_MAX_DELAY))
    model = (entry.get("model") or "").lower()
    enforce = normalize_enforce(entry.get("enforce", dflt.get("enforce")))
    print(f"matched rule:   {matched}")
    print(f"  paced         {entry.get('paced', False)}")
    print(f"  model         {entry.get('model') or '<undeclared>'}")
    print(f"  m0 / m1       {m0} / {m1}")
    print(f"  max_delay     "
          f"{human_delta(max_delay) if max_delay is not None else 'no limit'}")
    print(f"  enforces      {', '.join(sorted(enforce))}")

    if not os.path.exists(STATE_PATH):
        print("\nno snapshot yet -- is `niceclaude watch` running?")
        return 0
    with open(STATE_PATH, encoding="utf-8") as fh:
        st = json.load(fh)
    now = time.time()
    age = int(now) - st["ts_epoch"]
    print(f"\nsnapshot age:   {age}s")
    if age > MAX_STALE:
        print(f"  WARNING: older than {MAX_STALE}s. The daemon is probably not "
              f"running.\n"
              f"           Paced folders still self-heal (the hook refreshes on\n"
              f"           demand, costing ~2s on that tool call), but nothing is\n"
              f"           sampling while you are idle, so `burn` and `plot` will\n"
              f"           be biased. Start it with: niceclaude watch")
    # Every bucket is judged, including the ones this folder ignores. Which
    # line is the painful one is not obvious in advance -- the weekly line
    # rises at 0.60 %/h against the session line's 20 %/h, so the two produce
    # waits that differ by orders of magnitude -- and seeing all three is how
    # you decide whether the `--enforce` set is the one you want.
    #
    # "ENFORCED" has to mean acted on, not merely listed in `--enforce`. A
    # folder switched off with `niceclaude off`, or a `global off`, still has
    # its rule and its enforce set on file -- so reading the set alone printed
    # ENFORCED/HOLDS for a folder the hook returns from immediately. The verdict
    # line below said "not paced" while the table above it said the opposite,
    # and the table is the part that gets scanned.
    active = genabled and entry.get("paced", False)
    if not active:
        # Otherwise the rule block says `enforces session` two lines up while
        # every row reads `ignored`, which looks like a bug rather than the
        # switch being off.
        why = "paced false" if genabled else "global.enabled false"
        print(f"  ({why} -- nothing below is acted on; shown as it would be "
              f"judged)")
    for k, b in st["buckets"].items():
        enforced = active and (
            (k == "session" and "session" in enforce)
            or (k == "week:all models" and "week" in enforce)
            or ("model" in enforce and model_matches(k, model)))
        mark = "ENFORCED" if enforced else "ignored "
        p = bucket_pace(b, now, m0, m1)
        if p is None:
            print(f"  {k:22} unusable (no percentage)")
            continue
        if p["elapsed"] is None:
            # No reset clause yet, so f_t is unknown and bucket_pace judged
            # against the m0 floor. The line cannot be solved for a wake time.
            where = f"line {m0:5.1f}% (floor) | window start unknown"
        else:
            where = (f"line {p['allowed']:5.1f}% "
                     f"| {p['elapsed'] * 100:5.1f}% elapsed")
        print(f"  {k:22} {p['pct']:>3.0f}% used | {where} | {mark} "
              f"| {describe_hold(p, enforced, chunk)}")

    # What the hook itself would decide, from this same snapshot -- asked of
    # the hook rather than recomputed, so `status` cannot claim a folder is
    # running while the hook is holding it.
    d = hook.decide(pol, st, key, now, degraded=age > MAX_STALE)
    print()
    if not d.get("paced"):
        print("right now:      not paced -- the hook returns immediately here")
        return 0
    if not d.get("braked"):
        print("right now:      running -- no enforced line is over")
        return 0

    reason = d.get("reason", "")
    wake_at = d.get("wake_at", now)
    chunk = d.get("chunk", chunk)
    # The sleep is chunked, so a frozen agent re-reads policy -- and re-derives
    # this wait -- far more often than the wait itself is long. That is what
    # lets `global off` free an already-frozen agent within one chunk.
    nap = min(chunk, max(1.0, wake_at - now))
    if max_delay is not None:
        nap = max(1.0, min(nap, max_delay))
    if d.get("blind"):
        why = reason[len("BLIND: "):] if reason.startswith("BLIND: ") else reason
        print(f"right now:      BRAKED, blind -- {why}")
        if age > MAX_STALE:
            # `status` has not attempted the refresh the hook tries first, so
            # for a stale snapshot this is a conditional, not a verdict.
            print("                The hook refreshes on demand before deciding, so it")
            print("                only actually holds here if that refresh fails.")
        print(f"                No release to solve for; it re-checks every "
              f"{human_delta(chunk)}.")
        if max_delay is not None:
            print(f"                max_delay caps the hold at "
                  f"{human_delta(max_delay)}, then it proceeds anyway.")
    else:
        when = datetime.fromtimestamp(wake_at).strftime("%a %H:%M")
        print(f"right now:      BRAKED -- {reason}")
        # With max_delay set the hook stops holding long before the line
        # catches up, so reporting the solved release as the wait would
        # overstate it by hours. Report what it will actually do.
        if max_delay is not None and wake_at - now > max_delay:
            print(f"                holds {human_delta(max_delay)} (max_delay), then "
                  f"proceeds while still over")
            print(f"                the line; the line itself clears in "
                  f"{human_delta(wake_at - now)} ({when} local).")
            print(f"                Each later tool call brakes again for up to "
                  f"{human_delta(max_delay)}.")
            print("                That clearing time is not a promise: every session")
            print("                draws on the same account-wide budget, so it can")
            print("                move out -- but the hold above is a timer and will")
            print("                not.")
        else:
            print(f"                releases in {human_delta(wake_at - now)} ({when} "
                  f"local); next check in {human_delta(nap)}")
            print("                That release is not a promise: every session draws on")
            print("                the same account-wide budget, so it can move out.")

    reserve = entry.get("fanout_reserve",
                        dflt.get("fanout_reserve", DEFAULT_FANOUT_RESERVE))
    if reserve:
        ds = hook.decide(pol, st, key, now, degraded=age > MAX_STALE,
                         event="SubagentStart")
        if ds.get("braked"):
            extra = human_delta(ds.get("wake_at", now) - now)
            print(f"                A SubagentStart is held to m1 {m1}+{reserve} "
                  f"and waits {extra}.")
    return 0


BIN_MINUTES = 15  # smoothing window for burn-rate estimates


def cmd_burn(bin_minutes):
    """Characterize consumption rate, and derive the duty cycle it implies.

    Instantaneous rates are meaningless here: with 1% quantization and 60s
    sampling, a single tick reads as 60%/hour. So samples are binned before
    differencing, and rates are reported as a distribution rather than a number.

    Two rates matter and they answer different questions:
      average (idle included) -- what you are actually spending
      peak    (busy bins)     -- what heavy work costs while it runs
    The second one sets the duty cycle: how much of the clock a paced agent can
    actually be working.
    """
    records = load_log()
    if len(records) < 2:
        print("not enough samples yet")
        return 1

    # bucket -> window-start -> {bin_index: (first_pct, last_pct)}
    series = {}
    for rec in records:
        if rec["exit_code"] != 0:
            continue
        when = datetime.fromtimestamp(rec["ts_epoch"], timezone.utc)
        buckets, _unparsed, _info = parse_usage(rec["raw"], when)
        for key, b in buckets.items():
            if b["pct"] is None or b["window_seconds"] is None:
                continue
            # Segment by window identity, so a roll starts a fresh series
            # instead of registering as a huge negative jump.
            seg = b["resets_epoch"] if b["resets_epoch"] is not None else "unknown"
            slot = series.setdefault(key, {}).setdefault(seg, {})
            idx = rec["ts_epoch"] // (bin_minutes * 60)
            if idx in slot:
                slot[idx][1] = b["pct"]
            else:
                slot[idx] = [b["pct"], b["pct"]]

    gaps = sorted(b["ts_epoch"] - a["ts_epoch"]
                  for a, b in zip(records, records[1:]))
    median_gap = gaps[len(gaps) // 2] if gaps else 0
    print(f"burn rate over {len(records)} samples, {bin_minutes}-minute bins")
    print(f"median sampling interval: {median_gap}s\n")
    if median_gap > 150:
        # The daemon polls every 60s. A much larger median means most samples
        # came from the hook's on-demand refresh, which only fires on paced
        # folders while work is actually happening -- so idle time is simply
        # absent from the record and every "average" below is inflated.
        print("  WARNING: sampling looks activity-driven, not continuous.\n"
              "           These figures see only periods when paced work was\n"
              "           running, so idle time is missing and the average rates\n"
              "           below are overstated. Run `niceclaude watch` for a\n"
              "           representative baseline.\n")
    for key in sorted(series):
        rates, total_delta, total_hours = [], 0.0, 0.0
        window_seconds = None
        for seg, slot in series[key].items():
            idxs = sorted(slot)
            for a, b in zip(idxs, idxs[1:]):
                gap_h = (b - a) * bin_minutes / 60.0
                if gap_h <= 0:
                    continue
                delta = slot[b][1] - slot[a][1]
                if delta < 0:      # window rolled inside a segment; skip
                    continue
                rates.append(delta / gap_h)
                total_delta += delta
                total_hours += gap_h
            window_seconds = window_seconds or (5 * 3600 if key == "session" else 7 * 86400)

        if not rates or total_hours <= 0:
            print(f"  {key:22} insufficient data")
            continue

        rates.sort()
        avg = total_delta / total_hours
        busy = [r for r in rates if r > 0]
        peak = busy[int(len(busy) * 0.9)] if busy else 0.0
        line_rate = 100.0 / (window_seconds / 3600.0)   # %/h the pace line rises

        print(f"  {key}")
        print(f"    observed span     {total_hours:.1f}h, {total_delta:.0f} points consumed")
        print(f"    average rate      {avg:.2f} %/h  (idle included)")
        print(f"    busy-bin p90      {peak:.2f} %/h  ({len(busy)}/{len(rates)} bins active)")
        print(f"    pace line rises   {line_rate:.2f} %/h")
        if peak > 0:
            duty = min(1.0, line_rate / peak)
            print(f"    implied duty cycle {duty * 100:.0f}%  "
                  f"(~{duty * 60:.0f} min of work per hour at this intensity)")
        if avg > 0:
            print(f"    at average rate, a full window lasts {100 / avg:.1f}h "
                  f"of wall clock")
        print()
    return 0


def cmd_list():
    pol = load_policy()
    print(f"global.enabled: {pol.get('global', {}).get('enabled', True)}")
    print(f"defaults:       {json.dumps(pol.get('defaults', {}))}")
    if not pol.get("paths"):
        print("no folders configured")
        return 0
    for k in sorted(pol["paths"]):
        print(f"  {k}  {json.dumps(pol['paths'][k])}")
    return 0


def installed_version():
    """The version of the installed niceclaude distribution, or None.

    Read from distribution metadata rather than a literal in the source, so
    pyproject.toml stays the only place the number is stored. Two copies of a
    version cannot be kept in agreement by hand, and the one check that exists
    -- publish.yml comparing the git tag against pyproject.toml -- would not
    catch a second copy drifting.

    importlib.metadata is imported inside the function, not at module scope.
    The lookup walks sys.path for dist-info and measured ~200ms on Windows
    here; every command loads this module and only this one needs the number.
    """
    from importlib.metadata import PackageNotFoundError, version
    try:
        return version("niceclaude")
    except PackageNotFoundError:
        return None


def cmd_version():
    """Print the installed version.

    Reports what is *installed*, which is the question worth answering: a stale
    build with a current checkout beside it is the failure this command exists
    to make visible, and a number read out of the source tree could not tell
    the two apart.

    Exits nonzero when there is no installed distribution to ask -- a source
    tree on sys.path with nothing installed, or an editable install that has
    been removed. Printing a guess there would misreport exactly the situation
    the command is for.
    """
    found = installed_version()
    if found is None:
        print("niceclaude: no installed distribution found -- running from a "
              "source tree?", file=sys.stderr)
        return 1
    print(f"niceclaude {found}")
    return 0


class _VersionAction(argparse.Action):
    """`--version`, without charging every other command for the lookup.

    argparse's built-in "version" action wants the string when the parser is
    built, so it would run installed_version() on every invocation. This defers
    it to the one call that actually prints something.
    """

    def __init__(self, option_strings, dest, help=None):
        super().__init__(option_strings, dest, nargs=0,
                         default=argparse.SUPPRESS, help=help)

    def __call__(self, parser, namespace, values, option_string=None):
        parser.exit(cmd_version())


# --- help text ---------------------------------------------------------------
#
# One entry per subcommand: the one-line summary the overview lists, the page
# that `niceclaude help <command>` and `niceclaude <command> --help` print, and
# optional examples printed after the options. Kept together, and apart from
# the parser, so the prose reads as prose. build_parser() looks every command
# up here, so a command with no entry cannot be built at all.
#
# The text is printed verbatim (RawDescriptionHelpFormatter), so it is wrapped
# here at 79 columns and the paragraphs are left as written. Argument help is
# different: argparse re-wraps it, so those strings live beside the arguments.

COMMAND_HELP = {
    "install": dict(
        summary="register the hook in Claude Code's settings",
        description="""\
Register the hook in Claude Code's user settings.

Merges a niceclaude-hook entry onto the PreToolUse and SubagentStart events in
Claude Code's settings.json (under ~/.claude, or CLAUDE_CONFIG_DIR). The rest
of that file -- your model, permissions, and other hooks on the same events --
is preserved, a file that cannot be parsed is refused rather than overwritten,
and running install twice updates the entry in place instead of registering
it twice.

Once registered, every session consults policy.json on every tool call. A
folder with no matching rule costs ~20ms and is otherwise untouched, so turning
pacing on or off is afterwards only ever `niceclaude on` / `niceclaude off`.
Set NICECLAUDE_OFF=1 in a shell to exempt the sessions started from it.

The same registration is also written as a standalone fragment (path printed)
that `claude --settings <fragment>` applies to one session, for anyone who
would rather opt sessions in than carry the hook everywhere.

policy.json is created with defaults if it does not exist, and otherwise left
alone unless you pass --force.
""",
        examples="""\
examples:
  niceclaude install
  niceclaude install --force      # start over with a default policy.json
"""),

    "uninstall": dict(
        summary="un-register the hook, keeping policy and logs",
        description="""\
Un-register the hook, keeping policy and logs.

Removes exactly the niceclaude-hook entries `install` added to Claude Code's
settings.json and leaves the rest of the file as it was. The settings fragment
is deleted too. policy.json, usage.jsonl and state.json are kept, so a later
`niceclaude install` picks up the same folders and margins.

This stops pacing everywhere at once. To stop it for one folder use
`niceclaude off`; to suspend every rule while keeping the hook registered use
`niceclaude global off`.
"""),

    "version": dict(
        summary="print the installed version",
        description="""\
Print the installed version.

The number is read from the installed distribution's metadata, not from the
source tree, so it reports what is actually running. A stale build beside a
newer checkout looks identical in every other way; this is how the two are
told apart. Exits nonzero when no distribution is installed at all.
`niceclaude --version` prints the same thing.
"""),

    "watch": dict(
        summary="poll usage forever (the daemon)",
        description="""\
Poll usage forever: the daemon.

Every --interval seconds, run `claude -p /usage`, append the verbatim output
and its parsed buckets to usage.jsonl, and publish the parsed snapshot to
state.json for the hook to read. A failed poll is logged and the previous
snapshot is left to age out rather than overwritten; the hook treats a
snapshot older than 180s as stale and refreshes it on demand.

Runs in the foreground and writes a pidfile, so a second copy refuses to
start. Stop it with `niceclaude stop`. deploy/ has a systemd user unit, a
container entrypoint, and a Windows Scheduled Task script for keeping it
running.

Pacing works without the daemon -- the hook refreshes a stale snapshot itself,
costing ~2s on that one tool call -- but only the daemon records idle time, so
`burn` and `plot` are biased without it.
""",
        examples="""\
examples:
  niceclaude watch                 # foreground; Ctrl-C or `niceclaude stop`
  niceclaude watch --interval 120
"""),

    "sample": dict(
        summary="one poll, printed and logged",
        description="""\
Take one poll, print it, and log it.

Runs `claude -p /usage` once, appends the record to usage.jsonl, and prints
the parsed record as JSON (everything but the verbatim raw text). It does NOT
publish to state.json, so the hook never sees it; use `refresh` for that.

Exits nonzero if the poll failed or any line went unparsed, which makes this
the quickest check that the parser still understands what `/usage` prints.
"""),

    "refresh": dict(
        summary="one poll, written to the state file",
        description="""\
Take one poll and publish it to the state file.

Runs `claude -p /usage` once, appends the record to usage.jsonl, and if it
parsed writes the snapshot to state.json, which is what the hook reads. This
is the command the hook itself runs when it finds the snapshot older than
180s, and it is what keeps pacing working with no daemon.

Exits nonzero if the poll failed or produced no buckets; the old snapshot is
then left alone.
"""),

    "check": dict(
        summary="run misparse assertions over the log",
        description="""\
Run misparse assertions over the whole log.

Every sample stores the verbatim `/usage` output, so this re-parses the entire
history with the current parser and reports anything that does not hold:
lines that went unparsed, percentages or reset clauses that could not be read,
and usage that DECREASED inside a window without the window rolling -- which
never happens, so it can only be a misparse.

Run it after changing the parser, or when a snapshot looks wrong. Exits
nonzero when there are problems, or when there are no records to check.
"""),

    "stop": dict(
        summary="stop the running daemon",
        description="""\
Stop the running daemon.

Reads the pidfile `watch` wrote and terminates that process (SIGTERM, or
taskkill on Windows). Prints "no daemon running" and exits 0 if there is none,
including when the pidfile is left over from a process that has already gone.

Use this rather than killing by name: any shell whose command line merely
mentions `niceclaude watch` matches the same pattern, so a name-based kill can
take out its own wrapper.
"""),

    "on": dict(
        summary="pace a folder and its subfolders",
        description="""\
Pace a folder and everything under it.

Writes a rule for PATH into policy.json. A rule matches the folder and every
subfolder, by path component (so /a/b never matches /a/bc), and the longest
matching rule wins: a deeper `on` or `off` overrides a shallower one, and the
filesystem root is a valid catch-all. The hook re-reads policy.json on every
tool call, so the change reaches an already-running agent at its next
checkpoint.

Running `on` again for a path that already has a rule changes only the
settings you name and keeps the rest. Settings a rule does not carry fall back
to the `defaults` block of policy.json (initially m0 5, m1 8, no cap), which
`niceclaude list` prints.

Each window's pace line is  allowed = m0 + f_t * (100 - m0 - m1),  where f_t
is the fraction of the window's time elapsed. Usage over the line brakes the
folder until the line catches up.

The hook cannot discover the running model, so the per-model weekly bucket is
enforced only when --model is declared.
""",
        examples="""\
examples:
  niceclaude on ~/projects/nightly --model opus
  niceclaude on ~/projects/alpha --model opus --enforce session
  niceclaude on ~/projects/nightly --max-delay 240
  niceclaude on / --model opus              # pace everything, then carve
  niceclaude off ~/projects/urgent          #   out what should run free
"""),

    "off": dict(
        summary="stop pacing a folder",
        description="""\
Stop pacing a folder, and everything under it.

Writes a rule for PATH with paced false. Because the longest matching rule
wins, this is also how a subtree is carved out of a paced parent: `on ~/work`
then `off ~/work/vendor` paces everything under ~/work except vendor. The rule
is kept rather than deleted, so a later `niceclaude on` for the same path
restores it with its settings intact. Running agents see the change at their
next tool call.

To release every folder at once without touching any rule, use `niceclaude
global off`. To exempt one session rather than one folder, start it with
NICECLAUDE_OFF=1.
""",
        examples="""\
examples:
  niceclaude off ~/projects/nightly
  niceclaude off ~/projects/nightly/vendor  # carve a subtree out of a rule
"""),

    "global": dict(
        summary="master switch for every folder",
        description="""\
Master switch for every folder.

`global off` suspends every rule at once; `global on` restores them. It is a
kill switch only and never enables pacing anywhere: a folder is paced if and
only if some rule matches it, and this switch just gates whether the rules are
consulted. It defaults to on, so `global on` is only ever the undo for an
earlier `global off`.

An agent the hook is already holding re-reads policy.json every 15s while it
waits, so `global off` frees it within that.

To pace everything, pace a folder that contains everything: `niceclaude on /`
(or `niceclaude on C:\\`) is a valid catch-all, and `off` rules below it still
carve out.
""",
        examples="""\
examples:
  niceclaude global off            # break glass: release everything
  niceclaude global on             # resume the rules as they were
"""),

    "status": dict(
        summary="explain the policy for a folder",
        description="""\
Explain the policy for a folder, and what the hook would do there right now.

For PATH (default: the current directory) this reports whether the hook is
registered at all, the global switch, which rule matched and its effective
settings (model, m0/m1, max_delay, enforced windows), and then every usage
bucket in the current snapshot: percent used, where the pace line is, and the
hold each one would impose, whether or not this folder enforces it. Seeing the
ignored buckets priced is how an --enforce choice is checked rather than
guessed at.

The final "right now" verdict comes from the hook's own decision function on
the same snapshot, so status cannot say running about a folder the hook is
holding. When braked it gives the reason, the release time, and how often the
hold is re-evaluated; with a max_delay set it says what the cap will actually
do.

Warns when the snapshot is older than 180s, which usually means `watch` is not
running. If NICECLAUDE_OFF is set in this shell it says so, instead of
claiming the folder is paced.
""",
        examples="""\
examples:
  niceclaude status                # the current directory
  niceclaude status ~/projects/nightly
"""),

    "list": dict(
        summary="show all configured folders",
        description="""\
Show all configured folders.

Prints the global switch, the `defaults` block, and every rule in policy.json
with its settings, sorted by path. This is the whole policy; `status` explains
how it applies to one folder.
"""),

    "burn": dict(
        summary="characterize burn rate and duty cycle",
        description="""\
Characterize burn rate, and the duty cycle it implies.

Reads usage.jsonl and reports, per bucket, how fast usage is consumed: the
average rate with idle time included (what you are actually spending), the
p90 of busy bins (what heavy work costs while it runs), and how fast the pace
line rises, for comparison. The ratio of the last two is the duty cycle: how
much of the clock a paced agent can actually be working.

Instantaneous rates are meaningless with 1% quantization and 60s sampling -- a
single tick reads as 60%/h -- so samples are binned (--bin-minutes) before
differencing. Needs at least two samples. Warns if the record looks
activity-driven rather than continuous, which is what happens without `watch`
running: idle time is then missing and every average is overstated.
"""),

    "plot": dict(
        summary="graph utilization against the pace line",
        description="""\
Graph recorded utilization against the pace line.

Reads usage.jsonl and draws, per window, the recorded utilization over the
pace line, marking where it ran over. This answers the question the whole tool
exists to serve: is consumption actually tracking the line, and when did it
run hot?

One panel per bucket `/usage` reports: the 5-hour session window, the shared
weekly window, and the per-model weekly window for each model that has a limit
of its own -- `week:Fable` and the like. That last one is usually the tightest
of the three, so a plot without it can look comfortable while the line that
actually brakes your agents is nearly spent. A final panel overlays every
window on its own progress, where the diagonal IS the pace line and anything
above it was over budget whichever window it came from.

Requires matplotlib, which is an optional extra:

    uv tool install "niceclaude[plot]"

The daemon and the hook never import it, so nothing else pays for it.
"""),

    "help": dict(
        summary="detailed help for one command",
        description="""\
Show detailed help for one command.

`niceclaude help <command>` prints that command's full page: what it does and
what it reads and writes, each argument with its default, and examples. It is
the same page as `niceclaude <command> --help`. With no command, it prints the
overview and the list of commands.
""",
        examples="""\
examples:
  niceclaude help                  # the overview
  niceclaude help on
  niceclaude help status
"""),
}


def _command(sub, name):
    """Add one subcommand, wired to its COMMAND_HELP entry."""
    spec = COMMAND_HELP[name]
    return sub.add_parser(
        name, help=spec["summary"], description=spec["description"],
        epilog=spec.get("examples"),
        formatter_class=argparse.RawDescriptionHelpFormatter)


def build_parser():
    """The parser, and the subcommand action whose `.choices` maps each command
    name to its own parser.

    Separate from main() so that `help` can print a sibling command's page,
    and so a test can walk every command and check that it has one.
    """
    ap = argparse.ArgumentParser(
        prog="niceclaude", description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--version", action=_VersionAction,
                    help="print the installed version and exit")
    sub = ap.add_subparsers(
        dest="cmd", required=True, metavar="<command>", title="commands",
        help="one of the commands below; `niceclaude help <command>` "
             "describes it in full")

    i = _command(sub, "install")
    i.add_argument("--force", action="store_true",
                   help="reset policy.json to the defaults as well; without "
                        "this an existing policy is kept")
    _command(sub, "uninstall")
    _command(sub, "version")
    w = _command(sub, "watch")
    w.add_argument("--interval", type=int, default=60, metavar="SECONDS",
                   help="seconds between polls (default: %(default)s); each "
                        "poll is one `claude -p /usage` call of about 2s")
    _command(sub, "sample")
    _command(sub, "refresh")
    _command(sub, "check")
    _command(sub, "stop")
    o = _command(sub, "on")
    o.add_argument("path",
                   help="the folder to pace; subfolders inherit the rule "
                        "unless a deeper rule overrides it")
    o.add_argument("--model",
                   help="the model sessions in this folder run as (opus, "
                        "sonnet, fable ...). Hooks are not told the model, so "
                        "it must be declared; it selects the per-model weekly "
                        "bucket, which goes unenforced without it")
    o.add_argument("--m0", type=float, metavar="PCT",
                   help="starting allowance, in percent of the window "
                        "(default: the policy's, initially 5). Without it "
                        "the pure diagonal would permit 0%% at 0%% elapsed "
                        "and nothing could ever begin")
    o.add_argument("--m1", type=float, metavar="PCT",
                   help="end-of-window reserve, in percent (default: the "
                        "policy's, initially 8). The line reaches 100 - m1 "
                        "at the window's end, so work comes in under the "
                        "wire rather than exactly on it")
    o.add_argument("--fanout-reserve", type=float, dest="fanout_reserve",
                   metavar="PCT",
                   help="extra reserve demanded of SubagentStart, on top of "
                        "m1 (default 0). Spawning a fan-out commits to far "
                        "more than one more step, so it can be held to a "
                        "higher bar while running agents finish")
    cap = o.add_mutually_exclusive_group()
    cap.add_argument("--max-delay", type=float, dest="max_delay",
                     metavar="SECONDS",
                     help="cap one hold at this many seconds, then proceed "
                          "while still over the line and brake again at the "
                          "next tool call. Keeps a wait shorter than the "
                          "prompt cache TTL, so the next turn is not re-read "
                          "from cold. Default: no cap, hold until the line "
                          "catches up")
    cap.add_argument("--no-max-delay", action="store_true", dest="no_max_delay",
                     help="remove the cap: hold until the line catches up. "
                          "Writes an explicit null, so it also overrides a "
                          "cap set in the policy's defaults")
    o.add_argument("--enforce", metavar="WINDOWS",
                   help="comma-separated windows to pace against: session "
                        "(the 5h window), week (the shared weekly window), "
                        "model (the per-model weekly window). Default: all "
                        "three. `status` prices the ignored ones too")
    f = _command(sub, "off")
    f.add_argument("path",
                   help="the folder to stop pacing; subfolders follow unless "
                        "a deeper rule overrides it")
    g = _command(sub, "global")
    g.add_argument("state", choices=["on", "off"],
                   help="off suspends every rule; on restores them")
    s = _command(sub, "status")
    s.add_argument("path", nargs="?", default=os.getcwd(),
                   help="the folder to explain (default: the current "
                        "directory)")
    _command(sub, "list")
    bn = _command(sub, "burn")
    bn.add_argument("--bin-minutes", type=int, default=BIN_MINUTES,
                    metavar="MINUTES",
                    help="width of the smoothing bin (default: %(default)s)")
    pl = _command(sub, "plot")
    pl.add_argument("-o", "--out", default="niceclaude-usage.png",
                    metavar="FILE",
                    help="where to write the image (default: %(default)s)")
    pl.add_argument("--m0", type=float, default=DEFAULT_M0, metavar="PCT",
                    help="m0 of the line to draw (default: %(default)s). "
                         "Drawing only; the policy is unchanged")
    pl.add_argument("--m1", type=float, default=DEFAULT_M1, metavar="PCT",
                    help="m1 of the line to draw (default: %(default)s). "
                         "Drawing only; the policy is unchanged")
    h = _command(sub, "help")
    which = h.add_argument("command", nargs="?", metavar="command",
                           help="the command to describe; with none, print "
                                "the overview")
    # Validated against the finished list, so a typo is rejected with the
    # valid names rather than raising KeyError below. Includes `help` itself.
    which.choices = list(sub.choices)
    return ap, sub


def cmd_help(ap, sub, name):
    """`niceclaude help [command]`: the overview, or one command's full page.

    Prints exactly what `niceclaude <command> --help` prints, from the same
    parser object, so the two ways in cannot drift apart.
    """
    (ap if name is None else sub.choices[name]).print_help()
    return 0


def main(argv=None):
    ap, sub = build_parser()
    a = ap.parse_args(argv)
    if a.cmd == "help":
        return cmd_help(ap, sub, a.command)
    if a.cmd == "install":
        return cmd_install(a.force)
    if a.cmd == "uninstall":
        return cmd_uninstall()
    if a.cmd == "version":
        return cmd_version()
    if a.cmd == "watch":
        return cmd_watch(a.interval)
    if a.cmd == "sample":
        rec = sample_once()
        append_log(rec)
        print(json.dumps({k: v for k, v in rec.items() if k != "raw"}, indent=2))
        return 0 if rec["parse_ok"] else 1
    if a.cmd == "refresh":
        rec = sample_once()
        append_log(rec)
        if rec["exit_code"] == 0 and rec["buckets"]:
            publish_state(rec)
            return 0
        return 1
    if a.cmd == "check":
        return cmd_check()
    if a.cmd == "stop":
        return cmd_stop()
    if a.cmd == "on":
        return cmd_on(a.path, a.model, a.m0, a.m1, a.fanout_reserve, a.enforce,
                      a.max_delay, a.no_max_delay)
    if a.cmd == "off":
        return cmd_off(a.path)
    if a.cmd == "global":
        return cmd_global(a.state == "on")
    if a.cmd == "status":
        return cmd_status(a.path)
    if a.cmd == "list":
        return cmd_list()
    if a.cmd == "burn":
        return cmd_burn(a.bin_minutes)
    if a.cmd == "plot":
        from . import plot as plotmod
        series = plotmod.collect(load_log(), parse_usage)
        return plotmod.render(series, a.out, a.m0, a.m1)
    return 1


if __name__ == "__main__":
    sys.exit(main())
