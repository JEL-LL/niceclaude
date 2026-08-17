"""Make the in-repo package importable without installing it.

The project keeps its source under src/, so a plain `pytest` run cannot see
`niceclaude` unless src/ is on sys.path. Do it here rather than relying on an
editable install, so the suite works straight from a checkout.

NICECLAUDE_DIR is redirected before the import: _shared computes DATA_DIR at
module scope, and nothing in the suite should be able to touch the real
~/.local/share/niceclaude even by accident.
"""

import os
import sys
import tempfile

_SRC = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src")
if _SRC not in sys.path:
    sys.path.insert(0, _SRC)

os.environ.setdefault(
    "NICECLAUDE_DIR", os.path.join(tempfile.gettempdir(), "niceclaude-pytest"))
