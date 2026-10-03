"""Make the in-repo package importable without installing it.

The project keeps its source under src/, so a plain `pytest` run cannot see
`niceclaude` unless src/ is on sys.path. Do it here rather than relying on an
editable install, so the suite works straight from a checkout.

NICECLAUDE_DIR is redirected before the import: _shared computes DATA_DIR at
module scope, and nothing in the suite should be able to touch the real
~/.local/share/niceclaude even by accident.

CLAUDE_CONFIG_DIR is redirected for a sharper reason. `niceclaude install` now
edits Claude Code's own settings file, so an unredirected run of this suite
would rewrite the developer's live ~/.claude/settings.json. The tests below
assert that the merge preserves what it finds, but a test suite must not need
its subject to be correct in order to be safe to run.
"""

import os
import sys
import tempfile

_SRC = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src")
if _SRC not in sys.path:
    sys.path.insert(0, _SRC)

_TMP = tempfile.gettempdir()
os.environ.setdefault("NICECLAUDE_DIR", os.path.join(_TMP, "niceclaude-pytest"))
os.environ.setdefault("CLAUDE_CONFIG_DIR",
                      os.path.join(_TMP, "niceclaude-pytest-claude"))
os.environ.pop("NICECLAUDE_OFF", None)   # a developer's shell must not skew the suite

import pytest  # noqa: E402


def pytest_configure(config):
    # The repo has no pytest ini section to declare markers in.
    config.addinivalue_line(
        "markers", "real_account_key: do not pin ACCOUNT_KEY to \"\"")


@pytest.fixture(autouse=True)
def _default_account(request, monkeypatch, tmp_path):
    """Run every test as the default account, unless it says otherwise.

    The CLAUDE_CONFIG_DIR set above is never the default, so the real
    import-time ACCOUNT_KEY is a temp path. Under it every hand-written,
    unstamped snapshot in the suite would count as another account's and be
    ignored -- the legacy rule trusts a missing stamp under the default key
    only. A test about the key itself opts out with the `real_account_key`
    marker, and sets the key it means explicitly.

    The registry is pinned for everyone: `install` is called throughout the
    suite, and would otherwise pile entries up in the persistent
    NICECLAUDE_DIR.
    """
    from niceclaude import _shared, cli, hook
    if request.node.get_closest_marker("real_account_key") is None:
        for mod in (_shared, hook, cli):
            monkeypatch.setattr(mod, "ACCOUNT_KEY", "")
    monkeypatch.setattr(cli, "REGISTRY_PATH", str(tmp_path / "accounts.json"))
