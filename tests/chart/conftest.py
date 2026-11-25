"""The chart's render tests: each renders the product chart, or a development fixture's, and asserts on what it renders.

THIS DIRECTORY, IMPORTABLE. Files here share the memoised render by a bare import (`from test_invariants import
_rendered_docs`, `from chart_yaml import FAST_LOADER`), and under `--import-mode=importlib` that resolves only when this
directory is on the path. A conftest is loaded before collection begins, so the seam holds for a single file as for the
whole directory.
"""

import pathlib
import sys


sys.path.insert(0, str(pathlib.Path(__file__).parent))
