"""Descriptor-identity key primitives — validation + key predicates.

The shared kernel half of the doc-key contract: every group (viewer, search,
annotator) validates route keys against the descriptor's whitelist pattern and
renders identity predicates through here, so a future service split never
imports a sibling group for dataset access (LANCE_MEDIA_MERGE §4.4).
"""

import re
from collections.abc import Sequence

from common.core.exceptions import ValidationError
from common.lancekit.descriptor import Declared
from common.lancekit.predicate import and_, eq


def validate_doc_key(declared: Declared, doc_id: str) -> str:
    """Whitelist-validate a doc key against the descriptor's pattern — validation
    ONLY, returning the raw key.

    The returned value is stamped into stored row identity and deterministic tag
    ids, so it must never be SQL-escaped here; escaping happens where a predicate
    is rendered (``lancekit.predicate``). The pattern is still the first injection
    guard for every filter that interpolates the key.
    """
    if not re.fullmatch(declared.identity.doc_key_pattern, doc_id):
        raise ValidationError("invalid doc_id")
    return doc_id


def chunk_key_filter(declared: Declared, doc_id: str, rest: Sequence[int]) -> str:
    """SQL predicate for one row keyed by (doc key, *other identity fields).

    ``doc_id`` must already be validated (and is escaped here at render time);
    ``rest`` values pair positionally with the non-doc key fields (int-cast, so
    they can't inject).
    """
    identity = declared.identity
    clauses = [eq(identity.doc_key, doc_id)]
    others = [f for f in identity.key_fields if f != identity.doc_key]
    # strict=False: pairs positionally, tolerating identities with fewer
    # non-doc key fields than the legacy 3-segment route shape provides.
    clauses.extend(eq(field, int(value)) for field, value in zip(others, rest, strict=False))
    return and_(*clauses)
