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

from ._shared import (  # noqa: E402
    CONFIG_DIR, DATA_DIR, DEFAULT_M0, DEFAULT_M1, DEFAULT_POLICY, LOG_PATH,
    POLICY_PATH, SETTINGS_PATH,
    MAX_STALE, STATE_PATH, WINDOW_SECONDS, model_matches, norm_path,
    normalize_enforce,
)

# "Current session: 11% used · resets Aug 14, 8:10pm (UTC)"
# "Current week (all models): 14% used · resets Aug 16, 12am (UTC)"
# "Current session: 0% used"                 <- seen right after a window rolls
#
# The reset clause is OPTIONAL: immediately after a window resets, the server
# emits the bucket with no reset time at all. Requiring it turned a perfectly
# healthy 0%-used window into an unparsed line.
# Separator is U+00B7; tolerate ASCII variants in case the rendering changes.
LINE_RE = re.compile(
    r"^Current\s+(?P<window>session|week)"
    r"(?:\s*\((?P<label>[^)]*)\))?"
    r"\s*:\s*"
    r"(?P<pct>[<>]?\s*[\d.]+)\s*%\s*used"
    r"(?:\s*[·*|-]\s*resets\s+(?P<resets>.+?)"
    r"\s*\((?P<tz>[A-Z]{2,5})\))?"
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

HOOK_EVENTS = ["PreToolUse", "SubagentStart"]


def utcnow():
    return datetime.now(timezone.utc)


# --- parsing -----------------------------------------------------------------

def parse_reset(text, now):
    """'Aug 14, 8:10pm' -> aware UTC datetime, or None. Never raises.

    The year is absent from the output. Supply it explicitly rather than
    patching it in afterwards: year-less strptime is deprecated in 3.14+ and
    mishandles Feb 29. Reset times are always ahead of now, so try this year
    then next and keep whichever lands in the future.
    """
    text = text.strip().rstrip(".")
    for fmt in ("%b %d, %I:%M%p", "%b %d, %I%p", "%b %d %I:%M%p", "%b %d %I%p"):
        for year in (now.year, now.year + 1):
            try:
                dt = datetime.strptime(f"{year} {text}", f"%Y {fmt}")
            except ValueError:
                continue
            dt = dt.replace(tzinfo=timezone.utc)
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
        resets = parse_reset(resets_raw, now) if resets_raw else None
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
        proc = subprocess.run(["claude", "-p", "/usage"],
                              capture_output=True, text=True, timeout=120)
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
    /foo/bar would match /foo/barbaz and silently pace the wrong tree.
    """
    target = norm_path(path)
    best = None
    for raw_key in pol.get("paths", {}):
        key = norm_path(raw_key)
        if target == key or target.startswith(key + os.sep):
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
                ppct, preset, pts = prev[key]
                rolled = (
                    (b["resets_epoch"] is not None and preset is not None
                     and abs(b["resets_epoch"] - preset) > RESET_JITTER)
                    or b["resets_epoch"] is None   # post-roll, no clause yet
                    or b["pct"] == 0               # usage is only 0 at a window start
                )
                # Usage cannot fall inside a window. If it did and the window
                # did not roll, the parser is wrong.
                if not rolled and None not in (b["pct"], ppct) and b["pct"] < ppct:
                    problems.append(
                        f"{ts}  {key}: usage DECREASED {ppct}% -> {b['pct']}% with no "
                        f"window roll (prev {pts}) -- likely misparse")
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


def cmd_install(force):
    hook_exe = find_hook_exe()
    if not hook_exe:
        print("error: could not find the niceclaude-hook executable. Install the "
              "package first (uv tool install niceclaude).", file=sys.stderr)
        return 1

    os.makedirs(DATA_DIR, exist_ok=True)
    os.makedirs(CONFIG_DIR, exist_ok=True)

    if not os.path.exists(POLICY_PATH) or force:
        save_policy(json.loads(json.dumps(DEFAULT_POLICY)))

    # Hook commands are run through a shell, so a path containing spaces
    # (C:\Users\...\Program Files\...) has to be quoted or it parses as two
    # arguments and the hook silently never fires.
    command = f'"{hook_exe}"' if " " in hook_exe else hook_exe

    # A --settings fragment, NOT ~/.claude/settings.json. Hooks merge additively
    # across scopes and a narrower scope cannot un-register a broader one, so a
    # global install would silently pace foreground work too, with no way to
    # exempt it.
    settings = {"hooks": {
        ev: [{"matcher": "*", "hooks": [
            {"type": "command", "command": command, "timeout": 21600}]}]
        for ev in HOOK_EVENTS
    }}
    write_atomic(SETTINGS_PATH, json.dumps(settings, indent=2))

    print(f"hook:     {command}")
    print(f"settings: {SETTINGS_PATH}")
    print(f"policy:   {POLICY_PATH}")
    print("\nnext:")
    print("  niceclaude on <folder> --model opus")
    print("  niceclaude watch")
    print(f"  claude --settings {SETTINGS_PATH} ...")
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


def cmd_on(path, model, m0, m1, fanout_reserve, enforce):
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


def cmd_status(path):
    pol = load_policy()
    key = norm_path(path)
    matched, entry = resolve(pol, key)
    genabled = pol.get("global", {}).get("enabled", True)
    print(f"folder:         {key}")
    print(f"global.enabled: {genabled}")
    if matched is None:
        print("matched rule:   <none>  -> NOT paced")
        return 0
    d = pol.get("defaults", {})
    m0 = entry.get("m0", d.get('m0', DEFAULT_M0))
    m1 = entry.get("m1", d.get('m1', DEFAULT_M1))
    model = (entry.get("model") or "").lower()
    enforce = normalize_enforce(entry.get("enforce", d.get("enforce")))
    print(f"matched rule:   {matched}")
    print(f"  paced         {entry.get('paced', False)}")
    print(f"  model         {entry.get('model') or '<undeclared>'}")
    print(f"  m0 / m1       {m0} / {m1}")
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
    for k, b in st["buckets"].items():
        enforced = ((k == "session" and "session" in enforce)
                    or (k == "week:all models" and "week" in enforce)
                    or ("model" in enforce and model_matches(k, model)))
        if b["pct"] is None:
            print(f"  {k:22} unusable (no percentage)")
            continue
        if b["resets_epoch"] is None:
            # No reset clause yet. f_t is unknown, but allowed() is never below
            # m0, so m0 is the safe floor to judge against.
            hot = "  <-- OVER" if enforced and (b["pct"] + 1) > m0 else ""
            print(f"  {k:22} {b['pct']:>3.0f}% used | line {m0:5.1f}% (floor) "
                  f"| window start unknown | "
                  f"{'ENFORCED' if enforced else 'ignored '}{hot}")
            continue
        start = b["resets_epoch"] - b["window_seconds"]
        ft = (now - start) / b["window_seconds"]
        allowed = m0 + ft * (100 - m0 - m1)
        mark = "ENFORCED" if enforced else "ignored "
        hot = "  <-- OVER" if enforced and (b["pct"] + 1) > allowed else ""
        print(f"  {k:22} {b['pct']:>3.0f}% used | line {allowed:5.1f}% "
              f"| {ft * 100:5.1f}% elapsed | {mark}{hot}")
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


def main():
    ap = argparse.ArgumentParser(
        prog="niceclaude", description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)

    i = sub.add_parser("install", help="write the hook and settings fragment")
    i.add_argument("--force", action="store_true", help="reset policy.json too")
    w = sub.add_parser("watch", help="poll usage forever (the daemon)")
    w.add_argument("--interval", type=int, default=60)
    sub.add_parser("sample", help="one poll, printed and logged")
    sub.add_parser("refresh", help="one poll, written to the state file")
    sub.add_parser("check", help="run misparse assertions over the log")
    sub.add_parser("stop", help="stop the running daemon")
    o = sub.add_parser("on", help="pace a folder and its subfolders")
    o.add_argument("path"); o.add_argument("--model")
    o.add_argument("--m0", type=float); o.add_argument("--m1", type=float)
    o.add_argument("--fanout-reserve", type=float, dest="fanout_reserve",
                   help="extra reserve demanded of SubagentStart, on top of m1")
    o.add_argument("--enforce",
                   help="comma-separated windows to pace against: session, week, "
                        "model (default: all three)")
    f = sub.add_parser("off", help="stop pacing a folder")
    f.add_argument("path")
    g = sub.add_parser("global", help="master switch for every folder")
    g.add_argument("state", choices=["on", "off"])
    s = sub.add_parser("status", help="explain the policy for a folder")
    s.add_argument("path", nargs="?", default=os.getcwd())
    sub.add_parser("list", help="show all configured folders")
    bn = sub.add_parser("burn", help="characterize burn rate and duty cycle")
    bn.add_argument("--bin-minutes", type=int, default=BIN_MINUTES)
    pl = sub.add_parser("plot", help="graph utilization against the pace line")
    pl.add_argument("-o", "--out", default="niceclaude-usage.png")
    pl.add_argument("--m0", type=float, default=DEFAULT_M0)
    pl.add_argument("--m1", type=float, default=DEFAULT_M1)

    a = ap.parse_args()
    if a.cmd == "install":
        return cmd_install(a.force)
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
        return cmd_on(a.path, a.model, a.m0, a.m1, a.fanout_reserve, a.enforce)
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
