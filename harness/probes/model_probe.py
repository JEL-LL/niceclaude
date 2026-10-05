"""Phase 0 probe for open-questions.md section 9 (model detection).

Logs only what the plan needs: payload key NAMES, the event, tool_name,
agent_id, agent_type and a timestamp, and for each candidate transcript the
last three components of its path, whether it exists, its size, its
assistant-record count, its distinct message.model values, the last one read
backwards, and how long that read took. Never tool_input, never message
content. Results: harness/platform-findings.md section 16.

To re-run after a Claude Code upgrade, from an UNPACED scratch dir, save this
as probe.json:

    {"hooks": {
      "PreToolUse":    [{"matcher": "*", "hooks": [{"type": "command",
                         "command": "py <abs path>/model_probe.py", "timeout": 30}]}],
      "SubagentStart": [{"matcher": "*", "hooks": [{"type": "command",
                         "command": "py <abs path>/model_probe.py", "timeout": 30}]}]}}

Write <abs path> with FORWARD slashes, quoted if it has spaces. On Windows the
hook runs under Git Bash, where a backslash is an escape (see hook_command in
cli.py). Use python3 instead of py off Windows. Then run
`claude --settings probe.json -p "<prompt>"` with a prompt that runs one Bash
command and then one subagent that runs one. The probe appends to probe.log in
the hook's cwd, which is the session's scratch dir, so the log stays out of
the repo.
"""
import json, os, sys, time

LOG = os.path.abspath("probe.log")   # the cwd: an unpaced scratch dir


def last_model_backwards(path, block=8192):
    """The PoC's `tac | jq select(.type=="assistant") | head -1`, in Python."""
    with open(path, "rb") as fh:
        fh.seek(0, 2)
        pos = fh.tell()
        tail = b""
        while pos > 0:
            step = min(block, pos)
            pos -= step
            fh.seek(pos)
            buf = fh.read(step) + tail
            lines = buf.split(b"\n")
            tail = lines[0]          # may be partial; carried to next block
            for line in reversed(lines[1:]):
                m = _model_of(line)
                if m is not None:
                    return m
        return _model_of(tail)


def _model_of(line):
    if b'"assistant"' not in line:
        return None
    try:
        rec = json.loads(line)
    except ValueError:
        return None
    if rec.get("type") != "assistant":
        return None
    return (rec.get("message") or {}).get("model") or ""


def describe(path):
    out = {"path_tail": os.sep.join(path.split(os.sep)[-3:]) if path else None}
    if not path or not os.path.isfile(path):
        out["exists"] = False
        return out
    out["exists"] = True
    out["size"] = os.path.getsize(path)
    models = []
    with open(path, "rb") as fh:
        for line in fh:
            if b'"assistant"' in line:
                m = _model_of(line)
                if m is not None:
                    models.append(m)
    out["assistant_records"] = len(models)
    out["distinct_models"] = sorted(set(models))
    t0 = time.perf_counter()
    out["last_model"] = last_model_backwards(path)
    out["backwards_ms"] = round((time.perf_counter() - t0) * 1000, 2)
    return out


def main():
    p = json.loads(sys.stdin.read() or "{}")
    main_t = p.get("transcript_path") or ""
    sid, aid = p.get("session_id"), p.get("agent_id")
    rec = {
        "t": round(time.time(), 3),
        "event": p.get("hook_event_name"),
        "tool_name": p.get("tool_name"),
        "agent_id": aid,
        "agent_type": p.get("agent_type"),
        "payload_keys": sorted(p.keys()),
        "main": describe(main_t),
    }
    if aid:
        primary = os.path.join(os.path.dirname(main_t), sid, "subagents",
                               f"agent-{aid}.jsonl")
        rec["sub_primary"] = describe(primary)
        found = None
        for root, _dirs, files in os.walk(os.path.dirname(main_t)):
            if f"agent-{aid}.jsonl" in files:
                found = os.path.join(root, f"agent-{aid}.jsonl")
                break
        rec["sub_search_found_same"] = (found == primary) if found else None
    with open(LOG, "a", encoding="utf-8") as fh:
        fh.write(json.dumps(rec) + "\n")


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:  # a probe must never block the session
        with open(LOG, "a", encoding="utf-8") as fh:
            fh.write(json.dumps({"probe_error": repr(exc)}) + "\n")
    sys.exit(0)
