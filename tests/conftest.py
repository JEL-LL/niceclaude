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
