"""Account-keyed paths: one data directory per Claude config dir.

Every account used to share one state.json, so a hook in one account could pace
on another account's usage whenever that snapshot was fresh enough -- in both
directions, which is what made it worse than most bugs here. These tests pin
how a CLAUDE_CONFIG_DIR becomes a directory: the default account keeps exactly
the paths it always had, every other account gets its own under `accounts/`,
and the shared files stay at the root whatever the account.

The key has to follow Claude Code, not merely be self-consistent, because two
niceclaude processes that agree with each other but not with Claude are pacing
a directory Claude is not using. Hence the tests that a literal `~` is NOT
expanded: Claude does not expand it (harness/platform-findings.md section 15).

Import-time constants can only be observed in a fresh process, so the cases
that are about them run a child Python with a redirected environment. The home,
LOCALAPPDATA and APPDATA are always redirected under tmp_path, so nothing here
can resolve to, or write into, the developer's real directories.
"""

import json
import os
import subprocess
import sys
import types
import zlib

import pytest

from niceclaude import _shared, cli, hook
from niceclaude._shared import account_paths, account_slug, norm_config_dir

SRC = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                   "src")

PATHS_KEYS = ["config_key", "slug", "data_dir", "state_path", "log_path",
              "hook_log_path", "pid_path", "root_dir", "policy_path",
              "registry_path"]


def home_env(tmp_path, **extra):
    """An environment whose home and app dirs all sit under tmp_path, with
    NICECLAUDE_DIR and CLAUDE_CONFIG_DIR removed unless given in `extra`."""
    env = dict(os.environ)
    env.pop("NICECLAUDE_DIR", None)
    env.pop("CLAUDE_CONFIG_DIR", None)
    home = str(tmp_path / "home")
    env.update(HOME=home, USERPROFILE=home,
               LOCALAPPDATA=str(tmp_path / "localappdata"),
               APPDATA=str(tmp_path / "appdata"))
    env["PYTHONPATH"] = SRC + os.pathsep + env.get("PYTHONPATH", "")
    env.update(extra)
    return env


def old_data_dir(env, home):
    """The data-dir resolver exactly as it was before accounts, with
    os.environ and HOME made parameters. The default account must match it."""
    override = env.get("NICECLAUDE_DIR")
    if override:
        return override
    if os.name == "nt":
        base = env.get("LOCALAPPDATA") or os.path.join(home, "AppData", "Local")
        return os.path.join(base, "niceclaude")
    return os.path.join(home, ".local", "share", "niceclaude")


def old_config_dir(env, home):
    """The config-dir resolver exactly as it was before accounts, with
    os.environ and HOME made parameters. It was rewritten to take `env` in the
    same change, so the default account must match this too."""
    override = env.get("NICECLAUDE_CONFIG_DIR")
    if override:
        return override
    data_override = env.get("NICECLAUDE_DIR")
    if data_override:
        return os.path.join(data_override, "config")
    if os.name == "nt":
        base = env.get("APPDATA") or os.path.join(home, "AppData", "Roaming")
        return os.path.join(base, "niceclaude")
    return os.path.join(home, ".config", "niceclaude")


CONSTANTS = ("ROOT_DIR", "ACCOUNT_KEY", "DATA_DIR", "LOG_PATH", "STATE_PATH",
             "HOOK_LOG_PATH", "POLICY_PATH", "CLAUDE_SETTINGS_MARKER_PATH",
             "REGISTRY_PATH", "SETTINGS_PATH")


def child_constants(env):
    """_shared's import-time constants, and cli.PID_PATH, in a fresh process."""
    code = ("import json; from niceclaude import _shared, cli; "
            f"d = {{k: getattr(_shared, k) for k in {CONSTANTS!r}}}; "
            "d['PID_PATH'] = cli.PID_PATH; print(json.dumps(d))")
    out = subprocess.run([sys.executable, "-c", code], env=env,
                         capture_output=True, text=True, check=True)
    return json.loads(out.stdout)


def run_paths(env, *args):
    code = ("import sys; from niceclaude import cli; "
            f"raise SystemExit(cli.main(['paths', *{list(args)!r}]))")
    return subprocess.run([sys.executable, "-c", code], env=env,
                          capture_output=True, text=True)


# --- the default account keeps today's paths ----------------------------------

@pytest.mark.parametrize("config_dir", ["unset", "empty", "default"])
def test_default_account_keeps_the_legacy_paths(tmp_path, config_dir):
    home = tmp_path / "home"
    (home / ".claude").mkdir(parents=True)
    extra = {"unset": {}, "empty": {"CLAUDE_CONFIG_DIR": ""},
             "default": {"CLAUDE_CONFIG_DIR": str(home / ".claude")}}[config_dir]
    env = home_env(tmp_path, **extra)
    got = child_constants(env)
    legacy = old_data_dir(env, str(home))
    assert got["ACCOUNT_KEY"] == ""
    assert got["ROOT_DIR"] == got["DATA_DIR"] == legacy
    assert got["STATE_PATH"] == os.path.join(legacy, "state.json")
    assert got["LOG_PATH"] == os.path.join(legacy, "usage.jsonl")
    assert got["HOOK_LOG_PATH"] == os.path.join(legacy, "hook.log")
    assert got["POLICY_PATH"] == os.path.join(legacy, "policy.json")
    assert got["CLAUDE_SETTINGS_MARKER_PATH"] == os.path.join(
        legacy, "claude_settings_marker.json")
    assert got["PID_PATH"] == os.path.join(legacy, "daemon.pid")
    assert got["SETTINGS_PATH"] == os.path.join(
        old_config_dir(env, str(home)), "settings.json")
    assert account_paths(extra.get("CLAUDE_CONFIG_DIR"), env)["data_dir"] == legacy


def test_default_account_has_an_empty_slug(tmp_path):
    env = home_env(tmp_path)
    assert account_paths(None, env)["slug"] == ""
    assert account_paths(str(tmp_path / "home" / ".claude"), env)["slug"] == ""
    assert account_slug("") == ""


def test_the_home_comes_from_env_not_the_process(tmp_path):
    """Built from the process home, the default would be the developer's real
    ~/.claude, and a test could never tell."""
    env = home_env(tmp_path)
    assert account_paths(str(tmp_path / "home" / ".claude"), env)["config_key"] == ""
    real = os.path.join(_shared.HOME, ".claude")
    assert account_paths(real, env)["config_key"] == norm_config_dir(real)


@pytest.mark.parametrize("raw,want", [("/home/x", "/home/x"),
                                      ("/home/x//", "/home/x"),
                                      ("/", "/")])
def test_a_posix_home_is_read_as_expanduser_reads_it(monkeypatch, raw, want):
    """expanduser strips trailing slashes but keeps a bare "/", and the default
    account's paths were built from what it returned. The platform is stubbed
    for _shared alone, so this runs everywhere."""
    monkeypatch.setattr(_shared, "os", types.SimpleNamespace(name="posix"))
    assert _shared._env_home({"HOME": raw}) == want


def test_the_root_comes_from_env_not_the_import_time_root(tmp_path):
    env = home_env(tmp_path)
    assert account_paths(None, env)["data_dir"] == old_data_dir(
        env, str(tmp_path / "home"))
    assert account_paths(None, env)["data_dir"] != _shared.ROOT_DIR


# --- the key follows Claude ---------------------------------------------------

def test_a_literal_tilde_is_not_expanded(tmp_path, monkeypatch):
    """Claude reads `~/.claude` as a relative path and makes `<cwd>/~/.claude`,
    so this is a different, non-default account, even though the home's own
    .claude exists."""
    (tmp_path / "home" / ".claude").mkdir(parents=True)
    (tmp_path / "cwd").mkdir()
    monkeypatch.chdir(tmp_path / "cwd")
    got = account_paths("~/.claude", home_env(tmp_path))
    assert got["config_key"] == norm_config_dir(
        str(tmp_path / "cwd" / "~" / ".claude"))
    assert got["config_key"] != ""
    assert got["data_dir"].startswith(os.path.join(
        str(tmp_path / "localappdata" if os.name == "nt" else tmp_path / "home")))


def test_config_dirs_are_never_passed_through_expanduser(tmp_path, monkeypatch):
    """norm_path expands `~`, which is right for folders and wrong here; guard
    against it creeping back into the config-dir path."""
    monkeypatch.chdir(tmp_path)

    def boom(*_a, **_k):
        raise AssertionError("expanduser/norm_path used on a config dir")

    monkeypatch.setattr(_shared.os.path, "expanduser", boom)
    monkeypatch.setattr(_shared, "norm_path", boom)
    cwd = os.getcwd()
    assert norm_config_dir("rel") == os.path.normcase(os.path.normpath(
        os.path.realpath(os.path.join(cwd, "rel"))))
    env = home_env(tmp_path)
    for value in ("~/.claude", "rel", str(tmp_path / "work"), None, ""):
        account_paths(value, env)


def test_trailing_separator_gives_one_slug(tmp_path):
    env = home_env(tmp_path)
    plain = str(tmp_path / "work")
    assert (account_paths(plain, env)["slug"]
            == account_paths(plain + os.sep, env)["slug"]
            == account_paths(plain + "/", env)["slug"])


@pytest.mark.skipif(os.name != "nt", reason="case folds only on Windows")
def test_case_gives_one_slug_on_windows(tmp_path):
    env = home_env(tmp_path)
    plain = str(tmp_path / "Work")
    assert account_paths(plain, env)["slug"] == account_paths(
        plain.upper(), env)["slug"]


@pytest.mark.skipif(os.name != "nt", reason="drive-letter spelling")
def test_slash_direction_gives_one_key_on_windows(tmp_path):
    env = home_env(tmp_path)
    assert (account_paths("C:/Users/x/.claude-work", env)["config_key"]
            == account_paths("C:\\Users\\x\\.claude-work", env)["config_key"])


def test_a_symlink_to_the_default_is_the_default(tmp_path):
    home = tmp_path / "home"
    (home / ".claude").mkdir(parents=True)
    link = tmp_path / "link-to-claude"
    try:
        os.symlink(str(home / ".claude"), str(link), target_is_directory=True)
    except (OSError, NotImplementedError):
        # An unprivileged Windows account may not make symlinks, but it can
        # make a junction, which realpath resolves just the same. Skipping
        # instead would leave the realpath half of the default test untested
        # on exactly the platform the Phase 0 findings came from.
        if os.name != "nt":
            pytest.skip("symlinks are not permitted here")
        try:
            import _winapi
            _winapi.CreateJunction(str(home / ".claude"), str(link))
        except (ImportError, OSError):
            pytest.skip("neither symlinks nor junctions are permitted here")
    env = home_env(tmp_path)
    got = account_paths(str(link), env)
    assert got["config_key"] == ""
    assert got["data_dir"] == old_data_dir(env, str(home))


# --- slugs --------------------------------------------------------------------

def test_two_accounts_get_two_short_safe_directories(tmp_path):
    env = home_env(tmp_path)
    a = account_paths(str(tmp_path / ".claude-work"), env)
    b = account_paths(str(tmp_path / "other" / ".claude-work"), env)
    assert a["slug"] != b["slug"]
    root = old_data_dir(env, str(tmp_path / "home"))
    for got in (a, b):
        assert got["data_dir"] == os.path.join(root, "accounts", got["slug"])
        assert set(got["slug"]) <= set("abcdefghijklmnopqrstuvwxyz0123456789._-")
        assert len(got["slug"]) <= 41
        assert got["slug"].startswith("claude-work-")


def test_the_slug_rule_step_by_step(tmp_path):
    """Lowercase, strip leading dots, replace unsafe characters, collapse runs
    of `-`, cap at 32 -- then the CRC32 of the whole key."""
    key = norm_config_dir(str(tmp_path / ".My Claude  Work"))
    crc = format(zlib.crc32(key.encode("utf-8")), "08x")
    assert account_slug(key) == f"my-claude-work-{crc}"
    long_key = norm_config_dir(str(tmp_path / ("x" * 50)))
    assert account_slug(long_key).split("-")[0] == "x" * 32
    # No run of dashes where an unsafe edge meets the separator.
    for name, want in (("claude (work)", "claude-work"), ("(work)", "work")):
        k = norm_config_dir(str(tmp_path / name))
        c = format(zlib.crc32(k.encode("utf-8")), "08x")
        assert account_slug(k) == f"{want}-{c}"
    # Straight to account_slug, past norm_config_dir: on Windows its normcase
    # already lowercases the key, which would hide a missing .lower() here.
    k = "C:/x/.My Claude  Work"
    c = format(zlib.crc32(k.encode("utf-8")), "08x")
    assert account_slug(k) == f"my-claude-work-{c}"
    # A basename with nothing left in it is the CRC alone, never "-<crc>".
    k = "C:/x/..."
    assert account_slug(k) == format(zlib.crc32(k.encode("utf-8")), "08x")


# --- NICECLAUDE_DIR wins outright ----------------------------------------------

def test_niceclaude_dir_is_the_data_dir_but_the_key_is_still_kept(tmp_path):
    fixed = str(tmp_path / "fixed")
    work = str(tmp_path / ".claude-work")
    env = home_env(tmp_path, NICECLAUDE_DIR=fixed, CLAUDE_CONFIG_DIR=work)
    got = account_paths(work, env)
    assert got["data_dir"] == fixed
    assert got["config_key"] == norm_config_dir(work)
    assert got["slug"] == account_slug(norm_config_dir(work))
    consts = child_constants(env)
    assert consts["DATA_DIR"] == consts["ROOT_DIR"] == fixed
    assert consts["ACCOUNT_KEY"] == norm_config_dir(work)


def test_shared_files_do_not_move_with_the_account(tmp_path):
    home = tmp_path / "home"
    (home / ".claude").mkdir(parents=True)
    cases = [{}, {"CLAUDE_CONFIG_DIR": ""},
             {"CLAUDE_CONFIG_DIR": str(home / ".claude")},
             {"CLAUDE_CONFIG_DIR": str(tmp_path / "a")},
             {"CLAUDE_CONFIG_DIR": str(tmp_path / "b")}]
    shared = {json.dumps([c[k] for k in ("ROOT_DIR", "POLICY_PATH",
                                         "CLAUDE_SETTINGS_MARKER_PATH",
                                         "REGISTRY_PATH", "SETTINGS_PATH")])
              for c in (child_constants(home_env(tmp_path, **e)) for e in cases)}
    assert len(shared) == 1
    root = old_data_dir(home_env(tmp_path), str(home))
    assert json.loads(shared.pop())[1:4] == [
        os.path.join(root, "policy.json"),
        os.path.join(root, "claude_settings_marker.json"),
        os.path.join(root, "accounts.json")]


# --- pidfiles -------------------------------------------------------------------

def test_each_account_has_its_own_daemon_pidfile(tmp_path):
    env = home_env(tmp_path)
    a = account_paths(str(tmp_path / "a"), env)
    b = account_paths(str(tmp_path / "b"), env)
    assert a["data_dir"] != b["data_dir"]
    assert a["pid_path"] != b["pid_path"]
    root = old_data_dir(env, str(tmp_path / "home"))
    assert account_paths(None, env)["pid_path"] == os.path.join(root,
                                                                "daemon.pid")


def test_the_daemon_pidfile_is_in_the_account_dir():
    assert cli.PID_PATH == os.path.join(cli.DATA_DIR, "daemon.pid")


# --- account_paths is what the constants are ----------------------------------

def test_account_paths_agrees_with_the_import_time_constants():
    got = account_paths(os.environ.get("CLAUDE_CONFIG_DIR"))
    assert got["config_key"] == _shared.ACCOUNT_KEY
    assert got["data_dir"] == _shared.DATA_DIR
    assert got["state_path"] == _shared.STATE_PATH
    assert got["log_path"] == _shared.LOG_PATH
    assert got["hook_log_path"] == _shared.HOOK_LOG_PATH
    assert got["pid_path"] == cli.PID_PATH


def test_account_paths_agrees_with_a_fresh_process(tmp_path):
    work = str(tmp_path / ".claude-work")
    env = home_env(tmp_path, CLAUDE_CONFIG_DIR=work)
    consts = child_constants(env)
    got = account_paths(work, env)
    assert consts["ACCOUNT_KEY"] == got["config_key"] != ""
    assert consts["DATA_DIR"] == got["data_dir"]
    assert consts["STATE_PATH"] == got["state_path"]
    assert consts["PID_PATH"] == got["pid_path"]


# --- the hot path -------------------------------------------------------------

def test_the_default_hook_imports_neither_hashlib_nor_zlib(tmp_path):
    """The hook runs on every tool call. The slug's CRC is imported lazily and
    only for a non-default account, and hashlib is never used at all."""
    env = home_env(tmp_path)
    out = subprocess.run(
        [sys.executable, "-c",
         "import sys, niceclaude.hook; "
         "print([m for m in ('hashlib', 'zlib') if m in sys.modules])"],
        env=env, capture_output=True, text=True, check=True)
    assert out.stdout.strip() == "[]", out.stderr


def test_the_hook_log_makes_a_missing_account_dir(tmp_path, monkeypatch):
    """Nothing makes a non-default account's directory until install, watch,
    sample or a refresh that runs, and the lines worth having -- a refresh that
    could not start, a brake or fail-open before any refresh -- come exactly
    before that."""
    env = home_env(tmp_path)
    got = account_paths(str(tmp_path / ".claude-work"), env)
    assert not os.path.exists(got["data_dir"])
    monkeypatch.setattr(hook, "HOOK_LOG_PATH", got["hook_log_path"])
    hook.log("brake before any refresh")
    with open(got["hook_log_path"], encoding="utf-8") as fh:
        assert fh.read().endswith(" brake before any refresh\n")


# --- end to end -----------------------------------------------------------------

def test_refresh_publishes_into_the_accounts_own_directory(tmp_path):
    """A real `refresh` in a fresh process under a non-default account. The
    child fakes subprocess.run in-process: a `claude` stub on PATH would not be
    found on Windows, where a list argv with no shell never tries .cmd."""
    work = str(tmp_path / ".claude-work")
    env = home_env(tmp_path, CLAUDE_CONFIG_DIR=work)
    code = (
        "from niceclaude import cli\n"
        "class P:\n"
        "    stdout = 'Current session: 5% used \\u00b7 resets Aug 14, 8:10pm (UTC)'\n"
        "    stderr = ''\n"
        "    returncode = 0\n"
        "cli.subprocess.run = lambda *a, **k: P()\n"
        "raise SystemExit(cli.main(['refresh']))\n")
    proc = subprocess.run([sys.executable, "-c", code], env=env,
                          capture_output=True, text=True)
    assert proc.returncode == 0, proc.stderr
    want = account_paths(work, env)
    root = old_data_dir(env, str(tmp_path / "home"))
    assert want["state_path"] == os.path.join(root, "accounts", want["slug"],
                                              "state.json")
    assert os.path.exists(want["state_path"])
    assert os.path.exists(want["log_path"])
    assert not os.path.exists(os.path.join(root, "state.json"))


# --- niceclaude paths ------------------------------------------------------------

def test_paths_differ_per_account_and_match_account_paths(tmp_path):
    seen = []
    for name in ("a", "b"):
        cfg = str(tmp_path / name)
        env = home_env(tmp_path, CLAUDE_CONFIG_DIR=cfg)
        proc = run_paths(env)
        assert proc.returncode == 0, proc.stderr
        got = json.loads(proc.stdout)
        want = account_paths(cfg, env)
        for key, value in want.items():
            assert got[key] == value, key
        seen.append(got["data_dir"])
    assert seen[0] != seen[1]


def test_paths_prints_exactly_the_ten_keys(tmp_path):
    proc = run_paths(home_env(tmp_path))
    assert proc.returncode == 0, proc.stderr
    assert list(json.loads(proc.stdout)) == PATHS_KEYS
    assert list(cli.PATHS_KEYS) == PATHS_KEYS


def test_paths_with_a_key_prints_that_value_alone(tmp_path):
    env = home_env(tmp_path, CLAUDE_CONFIG_DIR=str(tmp_path / "a"))
    whole = json.loads(run_paths(env).stdout)
    proc = run_paths(env, "pid_path")
    assert proc.returncode == 0, proc.stderr
    assert proc.stdout.splitlines() == [whole["pid_path"]]
    bad = run_paths(env, "bogus")
    assert bad.returncode != 0
    assert bad.stdout == ""
