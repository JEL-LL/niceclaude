"""Per-call model detection: `--model detect` (model-detection-plan.md).

A folder declared Fable enforces `week:Fable` on its Opus main agent too, and
one declared Opus never enforces it on its Fable subagents. Under `detect` the
hook reads the caller's model from the caller's own transcript instead, so each
call answers to its own per-model bucket.

Two halves are tested. The detector, on transcript fixtures: which file it
opens, and that the backwards read survives everything a live, half-written
JSONL file can throw at it. And the wiring: that `decide` uses the detected
family only under `detect`, that `run` detects lazily and once, and that an
Opus parent and a Fable subagent in one folder are told apart end to end.
"""

import io
import json
import os

import pytest

from niceclaude import cli, hook
from niceclaude._shared import norm_path

NOW = 1_800_000_000
SESSION_WINDOW = 5 * 3600
WEEK_WINDOW = 7 * 86400
SID = "sess-1"
AID = "a1b2c3"


def rec(model, kind="assistant"):
    """One transcript line, as bytes, newline not included."""
    return json.dumps({"type": kind, "message": {
        "model": model, "content": [{"type": "text", "text": "hi"}]}}).encode()


def user():
    return json.dumps({"type": "user", "message": {"content": "go"}}).encode()


def write_lines(path, lines, sep=b"\n", trailing=True):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "wb") as fh:
        fh.write(sep.join(lines) + (sep if trailing else b""))
    return str(path)


@pytest.fixture
def project(tmp_path):
    """A Claude Code project dir: the main transcript, and the subagent one at
    the path the payload implies."""
    base = tmp_path / "projects" / "proj"
    main = write_lines(base / f"{SID}.jsonl", [user(), rec("claude-opus-5-5")])
    sub = base / SID / "subagents" / f"agent-{AID}.jsonl"
    return base, main, sub


def main_payload(main, event="PreToolUse"):
    return {"hook_event_name": event, "session_id": SID,
            "transcript_path": main}


def sub_payload(main, event="PreToolUse"):
    return dict(main_payload(main, event), agent_id=AID)


# --- model_family ------------------------------------------------------------

@pytest.mark.parametrize("model_id,family", [
    ("claude-opus-5-5", "opus"),
    ("claude-fable-5-1", "fable"),
    ("claude-fable-5", "fable"),
    ("claude-opus-4-8", "opus"),
    ("claude-opus-5", "opus"),
    ("claude-haiku-4-5-20251001", "haiku"),
    ("claude-sonnet-5-5", "sonnet"),
    ("claude-sonnet-5", "sonnet"),
    ("us.anthropic.claude-opus-5-5", "opus"),
    ("Claude_Fable_5", "fable"),
])
def test_model_family_maps_every_id_seen(model_id, family):
    assert hook.model_family(model_id) == family


@pytest.mark.parametrize("model_id", [
    "<synthetic>", "", None, 5, "gpt-5", "claude", "claude-5-5"])
def test_model_family_is_none_for_anything_else(model_id):
    assert hook.model_family(model_id) is None


def test_a_detected_family_matches_the_fable_bucket():
    """The reason model_family exists: the raw id is not a word of the label."""
    assert not hook.model_matches("week:Fable", "claude-fable-5-1")
    assert hook.model_matches("week:Fable", hook.model_family("claude-fable-5-1"))


# --- which transcript --------------------------------------------------------

def test_main_agent_reads_transcript_path(project):
    _base, main, _sub = project
    assert hook.detect_model(main_payload(main)) == "claude-opus-5-5"


def test_the_newest_assistant_record_wins(tmp_path):
    path = write_lines(tmp_path / "t.jsonl", [
        rec("claude-sonnet-5-5"), user(), rec("claude-opus-5-5"), user()])
    assert hook.detect_model(main_payload(path)) == "claude-opus-5-5"


def test_subagent_reads_its_own_transcript(project):
    _base, main, sub = project
    write_lines(sub, [user(), rec("claude-fable-5-1")])
    assert hook.detect_model(sub_payload(main)) == "claude-fable-5-1"


def test_subagent_falls_back_to_searching_the_project_dir(project):
    base, main, _sub = project
    write_lines(base / "elsewhere" / "deeper" / f"agent-{AID}.jsonl",
                [rec("claude-haiku-4-5-20251001")])
    assert hook.detect_model(sub_payload(main)) == "claude-haiku-4-5-20251001"


def test_a_subagent_never_borrows_its_parents_model(project):
    """D3. The parent's model is exactly the wrong answer for a subagent."""
    _base, main, _sub = project
    assert hook.detect_model(sub_payload(main)) is None


def test_subagent_start_touches_nothing(project, monkeypatch):
    """D2. The transcript does not exist yet, and the search would walk the
    whole project dir on every fan-out."""
    _base, main, sub = project
    write_lines(sub, [rec("claude-fable-5-1")])
    touched = []

    # Recorded before raising: detect_model swallows every exception, so the
    # raise alone would still come back None and prove nothing.
    def boom(*a, **k):
        touched.append(a)
        raise AssertionError("SubagentStart touched the filesystem")

    monkeypatch.setattr(hook, "open", boom, raising=False)
    monkeypatch.setattr(hook.os, "walk", boom)
    monkeypatch.setattr(hook.os.path, "isfile", boom)
    monkeypatch.setattr(hook, "_last_model", boom)
    assert hook.detect_model(sub_payload(main, "SubagentStart")) is None
    assert touched == []
    # The control: the same payload on PreToolUse does reach the filesystem.
    hook.detect_model(sub_payload(main, "PreToolUse"))
    assert touched


@pytest.mark.parametrize("payload", [
    None, [], {}, {"transcript_path": ""}, {"transcript_path": 5},
    {"transcript_path": "/nowhere/at/all.jsonl"},
])
def test_no_transcript_is_none(payload):
    assert hook.detect_model(payload) is None


def test_a_missing_file_is_none(tmp_path):
    assert hook.detect_model(main_payload(str(tmp_path / "gone.jsonl"))) is None


def test_an_empty_file_is_none(tmp_path):
    path = tmp_path / "t.jsonl"
    path.write_bytes(b"")
    assert hook.detect_model(main_payload(str(path))) is None


# --- the backwards read ------------------------------------------------------

def test_a_torn_last_line_is_skipped(tmp_path):
    """A write in progress leaves half a record at the end of the file."""
    path = tmp_path / "t.jsonl"
    path.write_bytes(rec("claude-opus-5-5") + b"\n"
                     + rec("claude-fable-5-1")[:40])
    assert hook.detect_model(main_payload(str(path))) == "claude-opus-5-5"


def test_synthetic_is_skipped_and_the_read_keeps_going(tmp_path):
    """D4. Accepting <synthetic> would match no bucket."""
    path = write_lines(tmp_path / "t.jsonl", [
        rec("claude-fable-5-1"), rec("<synthetic>"), user()])
    assert hook.detect_model(main_payload(path)) == "claude-fable-5-1"


def test_assistant_records_without_a_usable_model_are_skipped(tmp_path):
    path = write_lines(tmp_path / "t.jsonl", [
        rec("claude-fable-5-1"),
        json.dumps({"type": "assistant", "message": {"model": ""}}).encode(),
        json.dumps({"type": "assistant", "message": {"model": 7}}).encode(),
        json.dumps({"type": "assistant", "message": "assistant"}).encode(),
        b'["assistant"]',
        json.dumps({"type": "user", "message": {"model": "claude-opus-5",
                                                "role": "assistant"}}).encode(),
    ])
    assert hook.detect_model(main_payload(path)) == "claude-fable-5-1"


@pytest.mark.parametrize("block", [1, 7, 16, 100, 64 * 1024])
def test_a_record_split_across_blocks_is_reassembled(tmp_path, monkeypatch,
                                                     block):
    """Every block boundary lands somewhere inside a record at these sizes, and
    the first line of the file is only ever whole once the scan reaches it."""
    monkeypatch.setattr(hook, "DETECT_BLOCK", block)
    path = write_lines(tmp_path / "t.jsonl", [
        rec("claude-fable-5-1"), user(), user()])
    assert hook.detect_model(main_payload(path)) == "claude-fable-5-1"
    path = write_lines(tmp_path / "u.jsonl", [
        user(), rec("claude-opus-5-5"), user()], trailing=False)
    assert hook.detect_model(main_payload(path)) == "claude-opus-5-5"


def test_crlf_line_endings(tmp_path, monkeypatch):
    monkeypatch.setattr(hook, "DETECT_BLOCK", 9)
    path = write_lines(tmp_path / "t.jsonl",
                       [user(), rec("claude-fable-5-1"), user()], sep=b"\r\n")
    assert hook.detect_model(main_payload(path)) == "claude-fable-5-1"


def test_non_utf8_bytes_do_not_break_the_read(tmp_path):
    """A garbage line after the record, and a stray byte inside the record."""
    stray = rec("claude-fable-5-1").replace(b'"hi"', b'"h\xff\xfei"')
    path = write_lines(tmp_path / "t.jsonl", [
        stray, b'\x80\x81"assistant"\xc3\x28 {not json', user()])
    assert hook.detect_model(main_payload(path)) == "claude-fable-5-1"


def test_the_read_gives_up_at_the_cap(tmp_path, monkeypatch):
    """D6. Too far back is unknown, not a whole-file read."""
    # The only record starts at offset 0, so it is whole only once the very
    # first byte has been read: one byte short of the file is one too few.
    path = write_lines(tmp_path / "t.jsonl", [rec("claude-fable-5-1")]
                       + [user()] * 20)
    size = os.path.getsize(path)
    monkeypatch.setattr(hook, "DETECT_BLOCK", 16)
    monkeypatch.setattr(hook, "DETECT_CAP", size - 1)
    assert hook.detect_model(main_payload(path)) is None
    monkeypatch.setattr(hook, "DETECT_CAP", size)
    assert hook.detect_model(main_payload(path)) == "claude-fable-5-1"


def test_a_long_line_is_joined_once_not_once_per_block(tmp_path, monkeypatch):
    """Re-joining a growing carry on every block made a long line quadratic:
    a 7 MiB line took 238 ms. Pinned without a clock: every block the read
    gets back is a byte string that counts the bytes any `+` on it copies,
    and every byte string the record parser sees is measured. Linear work is
    about the file's size; the quadratic carry was about size**2 / 32.
    This pins the `+` spelling only: a carry re-joined per block with
    b"".join copies as much and would pass. No clock is used, so CI cannot
    flake on it."""
    long_line = b'{"type":"user","x":"' + b"a" * (64 * 1024) + b'"}'
    path = write_lines(tmp_path / "t.jsonl",
                       [rec("claude-fable-5-1"), long_line])
    size = os.path.getsize(path)
    monkeypatch.setattr(hook, "DETECT_BLOCK", 16)
    copied = []

    class Counted(bytes):
        def __add__(self, other):
            copied.append(len(self) + len(other))
            return Counted(bytes(self) + bytes(other))

    class Reader:
        def __init__(self, fh):
            self.fh = fh

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            self.fh.close()

        def seek(self, *a):
            return self.fh.seek(*a)

        def tell(self):
            return self.fh.tell()

        def read(self, n):
            return Counted(self.fh.read(n))

    parsed = []
    real_record = hook._record_model
    monkeypatch.setattr(hook, "open", lambda p, m: Reader(open(p, m)),
                        raising=False)
    monkeypatch.setattr(hook, "_record_model",
                        lambda ln: parsed.append(len(ln)) or real_record(ln))
    assert hook.detect_model(main_payload(path)) == "claude-fable-5-1"
    assert sum(copied) + sum(parsed) < 3 * size


def test_detection_never_raises(tmp_path, monkeypatch):
    """Raising would fail the hook open; anything at all is just unknown."""
    path = write_lines(tmp_path / "t.jsonl", [rec("claude-fable-5-1")])

    def boom(*a, **k):
        raise RuntimeError("anything")

    monkeypatch.setattr(hook, "_last_model", boom)
    assert hook.detect_model(main_payload(path)) is None
    deep = b'{"type":"assistant","x":' + b"[" * 100_000 + b"]" * 100_000 + b"}"
    monkeypatch.undo()
    path = write_lines(tmp_path / "deep.jsonl", [rec("claude-opus-5"), deep])
    assert hook.detect_model(main_payload(path)) == "claude-opus-5"


# --- decide: the family only counts under `detect` ---------------------------

def bucket(pct, window):
    """Halfway through its window, so the line stands near 48%."""
    return {"pct": pct, "resets_epoch": NOW + window // 2,
            "window_seconds": window, "label": None}


def snapshot(fable_pct=90):
    """Session and week far under their lines; Fable far over its own."""
    return {"ts_epoch": NOW, "buckets": {
        "session": bucket(10, SESSION_WINDOW),
        "week:all models": bucket(10, WEEK_WINDOW),
        "week:Fable": bucket(fable_pct, WEEK_WINDOW)}}


def rules(cwd, model, **extra):
    entry = {"paced": True, **extra}
    if model is not None:
        entry["model"] = model
    return {"paths": {cwd: entry}}


W = norm_path("/w")


@pytest.mark.parametrize("declared", ["detect", "DETECT", "Detect"])
def test_detect_paces_on_the_callers_family(declared):
    pol = rules(W, declared)
    fable = hook.decide(pol, snapshot(), W, NOW, caller_model="fable")
    assert fable["braked"] and "week:Fable" in fable["reason"]
    assert not hook.decide(pol, snapshot(), W, NOW, caller_model="opus")["braked"]


@pytest.mark.parametrize("caller", [None, ""])
def test_detect_with_no_caller_model_has_no_per_model_bucket(caller):
    """D1. Unknown is not a guess: session and week still apply."""
    pol = rules(W, "detect")
    assert not hook.decide(pol, snapshot(), W, NOW,
                           caller_model=caller)["braked"]
    st = snapshot()
    st["buckets"]["session"] = bucket(95, SESSION_WINDOW)
    d = hook.decide(pol, st, W, NOW, caller_model=caller)
    assert d["braked"] and "session" in d["reason"]
    assert "week:Fable" not in d["reason"]


@pytest.mark.parametrize("caller", [None, "opus", "fable"])
def test_a_declared_model_ignores_the_caller(caller):
    """`--model fable` behaves exactly as before, whoever is calling."""
    fable = hook.decide(rules(W, "fable"), snapshot(), W, NOW,
                        caller_model=caller)
    assert fable == hook.decide(rules(W, "fable"), snapshot(), W, NOW)
    assert fable["braked"] and "week:Fable" in fable["reason"]
    opus = hook.decide(rules(W, "opus"), snapshot(), W, NOW,
                       caller_model=caller)
    assert not opus["braked"]
    none = hook.decide(rules(W, None), snapshot(), W, NOW, caller_model=caller)
    assert not none["braked"]


@pytest.mark.parametrize("caller,event", [
    (None, None), ("opus", None), (None, "SubagentStart")])
def test_detect_enforcing_only_model_frees_a_caller_with_no_bucket(caller,
                                                                   event):
    """Nothing applies to an Opus call, which is not the same as "cannot see":
    it must not fall into the blind brake, uncapped by default."""
    pol = rules(W, "detect", enforce=["model"])
    d = hook.decide(pol, snapshot(), W, NOW, event=event, caller_model=caller)
    assert d["paced"] and not d["braked"] and not d["blind"]
    assert d["region"] == "free"


def test_detect_enforcing_only_model_still_brakes_fable_and_an_empty_snapshot():
    pol = rules(W, "detect", enforce=["model"])
    d = hook.decide(pol, snapshot(), W, NOW, caller_model="fable")
    assert d["braked"] and "week:Fable" in d["reason"] and not d["blind"]
    empty = {"ts_epoch": NOW, "buckets": {}}
    for caller in (None, "opus", "fable"):
        d = hook.decide(pol, empty, W, NOW, caller_model=caller)
        assert d["braked"] and d["blind"]


@pytest.mark.parametrize("enforce", [None, ["session", "week"],
                                     ["session", "model"]])
@pytest.mark.parametrize("caller", [None, "opus"])
@pytest.mark.parametrize("degraded", [False, True])
def test_detect_with_session_and_week_missing_still_brakes_blind(enforce,
                                                                 caller,
                                                                 degraded):
    """Only `model` enforced makes "no bucket" mean "nothing applies". With
    session or week enforced and missing (the cp1252 misparse leaves only
    week:Fable), it is "cannot see", exactly as under a declared model."""
    extra = {} if enforce is None else {"enforce": enforce}
    pol = rules(W, "detect", **extra)
    st = {"ts_epoch": NOW, "buckets": {"week:Fable": bucket(10, WEEK_WINDOW)}}
    d = hook.decide(pol, st, W, NOW, degraded=degraded, caller_model=caller)
    assert d["braked"] and d["blind"]


@pytest.mark.parametrize("caller", [None, "fable", "opus", "sonnet"])
@pytest.mark.parametrize("degraded", [False, True])
def test_detect_ignores_a_missing_per_model_row(caller, degraded):
    """D9: under `detect` enforcing only `model`, a caller whose row is absent
    -- even a Fable caller whose week:Fable may only be unrendered -- runs
    free rather than freezing. Other families' rows do not change that."""
    pol = rules(W, "detect", enforce=["model"])
    st = snapshot()
    del st["buckets"]["week:Fable"]
    for extra in ({}, {"week:Sonnet only": bucket(90, WEEK_WINDOW)}):
        st["buckets"].update(extra)
        d = hook.decide(pol, st, W, NOW, degraded=degraded,
                        caller_model=caller)
        # The one caller whose own row is present, and over its line, still
        # answers to it.
        assert d["braked"] == (caller == "sonnet" and bool(extra))


# --- run: lazy, once, and end to end -----------------------------------------

class Stop(BaseException):
    """Raised from the hold's sleep, to end a brake the test has seen."""


@pytest.fixture
def hk(tmp_path, monkeypatch):
    """A hook with its files, clock and log under the test's control."""
    policy = tmp_path / "policy.json"
    state = tmp_path / "state.json"
    monkeypatch.setattr(hook, "POLICY_PATH", str(policy))
    monkeypatch.setattr(hook, "STATE_PATH", str(state))
    monkeypatch.setattr(hook, "HOOK_LOG_PATH", str(tmp_path / "hook.log"))
    monkeypatch.setattr(hook, "ACCOUNT_KEY", "")
    monkeypatch.setattr(hook.time, "time", lambda: NOW)
    monkeypatch.setattr(hook, "snapshot_age", lambda st, now: 10)
    monkeypatch.setattr(hook, "refresh_snapshot", lambda: False)
    logs = []
    monkeypatch.setattr(hook, "log", logs.append)
    calls = []
    real = hook.detect_model
    monkeypatch.setattr(hook, "detect_model",
                        lambda p: calls.append(p) or real(p))
    sleeps = []

    def sleep(_s):
        sleeps.append(_s)
        if len(sleeps) >= 3:
            raise Stop()

    monkeypatch.setattr(hook.time, "sleep", sleep)
    state.write_text(json.dumps(snapshot()), encoding="utf-8")

    def set_policy(pol):
        policy.write_text(json.dumps(pol), encoding="utf-8")

    cwd = norm_path(str(tmp_path / "work"))
    return {"cwd": cwd, "policy": set_policy, "logs": logs, "calls": calls,
            "sleeps": sleeps}


def brakes(logs):
    return [m for m in logs if m.startswith(("brake", "throttle"))]


def test_an_opus_parent_runs_and_its_fable_subagent_brakes(hk, project):
    """The case section 9 exists for, through `run`."""
    _base, main, sub = project
    write_lines(sub, [user(), rec("claude-fable-5-1")])
    hk["policy"](rules(hk["cwd"], "detect"))

    assert hook.run(hk["cwd"], "PreToolUse", payload=main_payload(main)) == (
        None, "line-caught-up")
    assert brakes(hk["logs"]) == []

    with pytest.raises(Stop):
        hook.run(hk["cwd"], "PreToolUse", payload=sub_payload(main))
    (line,) = brakes(hk["logs"])
    assert line.startswith("brake ") and "week:Fable" in line
    assert line.endswith(" model=fable(detected)")
    # Three hold passes, one detection each invocation (D5).
    assert len(hk["sleeps"]) == 3 and len(hk["calls"]) == 2


def test_main_hands_the_payload_to_run(hk, project, monkeypatch):
    """The real entry point: stdin to brake."""
    _base, main, sub = project
    write_lines(sub, [rec("claude-fable-5-1")])
    hk["policy"](rules(hk["cwd"], "detect"))
    monkeypatch.delenv("NICECLAUDE_OFF", raising=False)
    payload = dict(sub_payload(main), cwd=hk["cwd"])
    monkeypatch.setattr("sys.stdin", io.StringIO(json.dumps(payload)))
    with pytest.raises(Stop):
        hook.main()
    assert brakes(hk["logs"])[0].endswith(" model=fable(detected)")


def test_subagent_start_under_detect_has_no_model_bucket(hk, project):
    """D2. It launches past the Fable line; its first tool call is held."""
    _base, main, sub = project
    write_lines(sub, [rec("claude-fable-5-1")])
    hk["policy"](rules(hk["cwd"], "detect"))
    assert hook.run(hk["cwd"], "SubagentStart",
                    payload=sub_payload(main, "SubagentStart")) == (
        None, "line-caught-up")


def test_a_declared_fable_folder_brakes_its_opus_parent_as_before(hk, project):
    _base, main, _sub = project
    hk["policy"](rules(hk["cwd"], "fable"))
    with pytest.raises(Stop):
        hook.run(hk["cwd"], "PreToolUse", payload=main_payload(main))
    assert brakes(hk["logs"])[0].endswith(" model=fable(declared)")
    assert hk["calls"] == []


@pytest.mark.parametrize("pol", [
    lambda cwd: {"paths": {}},
    lambda cwd: {"paths": {cwd: {"paced": False, "model": "detect"}}},
    lambda cwd: {"global": {"enabled": False},
                 "paths": {cwd: {"paced": True, "model": "detect"}}},
    lambda cwd: rules(cwd, "opus"),
    lambda cwd: rules(cwd, None),
    lambda cwd: rules(cwd, "detect", enforce=["session", "week"]),
])
def test_detection_runs_only_where_it_can_matter(hk, project, pol):
    """D5. Unpaced, declared, undeclared, or not enforcing the model window:
    no transcript is opened."""
    _base, main, sub = project
    write_lines(sub, [rec("claude-fable-5-1")])
    hk["policy"](pol(hk["cwd"]))
    hook.run(hk["cwd"], "PreToolUse", payload=sub_payload(main))
    assert hk["calls"] == []


def test_a_blind_detect_brake_says_model_none(hk, project):
    """A subagent with no assistant record yet: brakes on session alone, and
    the log says no model was in play."""
    _base, main, _sub = project
    hk["policy"](rules(hk["cwd"], "detect", m1=99, m0=0))
    with pytest.raises(Stop):
        hook.run(hk["cwd"], "PreToolUse", payload=sub_payload(main))
    line = brakes(hk["logs"])[0]
    assert line.endswith(" model=none") and "week:Fable" not in line


# --- cli wording (D8) --------------------------------------------------------

VERDICT_NOTE = "verdict below is for a caller with no per-model bucket"


def status_of(tmp_path, monkeypatch, capsys, model):
    proj = tmp_path / "proj"
    proj.mkdir()
    state = tmp_path / "state.json"
    policy = tmp_path / "policy.json"
    state.write_text(json.dumps(dict(snapshot(), ts="x", ok=True)),
                     encoding="utf-8")
    policy.write_text(json.dumps(rules(norm_path(str(proj)), model)),
                      encoding="utf-8")
    monkeypatch.setattr(cli, "POLICY_PATH", str(policy))
    monkeypatch.setattr(cli, "STATE_PATH", str(state))
    monkeypatch.setattr(hook, "POLICY_PATH", str(policy))
    monkeypatch.setattr(cli.time, "time", lambda: NOW + 10)
    cli.cmd_status(str(proj))
    return capsys.readouterr().out


def test_status_says_whom_the_verdict_is_for_under_detect(tmp_path,
                                                          monkeypatch, capsys):
    """Asked with no caller, the verdict says "running" while the `per call`
    Fable row would hold a Fable caller for days."""
    assert VERDICT_NOTE in status_of(tmp_path, monkeypatch, capsys, "detect")


def test_status_has_no_verdict_note_under_a_declared_model(tmp_path,
                                                           monkeypatch, capsys):
    assert VERDICT_NOTE not in status_of(tmp_path, monkeypatch, capsys, "fable")


def test_status_marks_model_buckets_per_call(tmp_path, monkeypatch, capsys):
    out = status_of(tmp_path, monkeypatch, capsys, "detect")
    rows = {ln.split()[0]: ln for ln in out.splitlines()
            if ln.startswith("  ") and "% used" in ln}
    assert "| per call |" in rows["week:Fable"]
    assert "would hold" not in rows["week:Fable"]
    assert "| ENFORCED |" in rows["session"]
    assert "| ENFORCED |" in rows["week:all"]
    # Same column as the marks it replaces.
    assert (rows["week:Fable"].index("| per call")
            == rows["session"].index("| ENFORCED"))


def test_on_without_a_model_suggests_detect(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(cli, "POLICY_PATH", str(tmp_path / "policy.json"))
    target = tmp_path / "proj"
    target.mkdir()
    cli.cmd_on(str(target), model=None, m0=None, m1=None, fanout_reserve=None,
               enforce=None, max_delay=None)
    out = capsys.readouterr().out
    assert "--model detect" in out and "cannot discover" not in out
