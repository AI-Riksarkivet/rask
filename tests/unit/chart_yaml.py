"""The loader the chart gates parse the rendered manifest with.

WHY IT IS NOT `yaml.CSafeLoader` SPELLED INLINE. That attribute exists only when PyYAML was built
against libyaml, and `uv.lock` carries an **sdist beside its 28 wheels** — so a platform with no
matching wheel builds from source and gets a PyYAML whose `CSafeLoader` is absent. Referencing it
directly would then raise `AttributeError` at import and turn every chart gate RED, which is a
catastrophic trade for what is purely a speed optimisation: a slow gate beats a broken one.

WHY THE SPEED MATTERS AT ALL, measured 2026-09-09 against the real render (2.3 MB, ~300 documents):
`yaml.safe_load_all` takes **1.51 s** and the C loader **0.14 s** — a 10x penalty paid on every parse,
and instrumentation attributed ~57 % of `tests/unit`'s wall clock to that one pure-Python parser. The
two loaders were compared document-by-document on the same render and returned equal results, so this
is a parser swap and not a semantic one.

DEGRADING IS NOT THE SAME AS SAYING NOTHING: `test_the_chart_gate_parses_with_libyaml.py` asserts the
fast loader is the one actually in use on THIS platform, so a machine that quietly lost libyaml reports
it as one failing test rather than as a suite that got three times slower for no visible reason.
"""

from __future__ import annotations

from typing import Any

import yaml


#: `yaml.CSafeLoader` where libyaml is available, `yaml.SafeLoader` where it is not. Same safe schema
#: either way — the C loader is not a laxer parser, only a faster one.
FAST_LOADER: Any = getattr(yaml, "CSafeLoader", yaml.SafeLoader)

#: Whether the fast path is actually in use here. Read by the gate that reports a lost libyaml.
LIBYAML_AVAILABLE: bool = FAST_LOADER is not yaml.SafeLoader
