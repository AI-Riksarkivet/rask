"""`LineageRepository` executes queries; it does not also DEFINE the two query languages it speaks.

The module carried ~250 lines of module-level query literals — an openCypher DSL for AGE and a second,
unrelated raw-Postgres SQL DSL for the durable `lineage_events` / `lineage_reads` feed — interleaved with
the class that runs them. Two languages and their executor in one file is what made it a 1500-line module
nobody reads top to bottom, and it hid the fact that the service talks to its Postgres TWICE, through two
different dialects, for two different stores.

The two dialects now live in `lineage.services.cypher` and `lineage.services.postgres`. This gate keeps
them there: a new query added back into the repository body would rebuild the pile one constant at a time.
"""

from __future__ import annotations

import ast
import inspect
import re
from pathlib import Path
from types import ModuleType

from lineage.services import cypher, postgres, repository


#: Words that only appear in a query. Deliberately broad — the point is that NO query language is defined
#: in the repository module, not that some particular statement is absent.
_QUERY_WORDS = re.compile(r"\b(MATCH|MERGE|SELECT|INSERT INTO|DELETE FROM|CREATE (TABLE|INDEX|UNIQUE)|DETACH DELETE|OPTIONAL MATCH)\b")


def _module_level_query_literals(module: ModuleType) -> dict[str, str]:
    source = Path(inspect.getfile(module)).read_text(encoding="utf-8")
    found: dict[str, str] = {}
    for node in ast.parse(source).body:
        if isinstance(node, ast.AnnAssign):
            targets, value = [node.target], node.value
        elif isinstance(node, ast.Assign):
            targets, value = node.targets, node.value
        else:
            continue
        if not isinstance(value, ast.Constant) or not isinstance(value.value, str) or not _QUERY_WORDS.search(value.value):
            continue
        for target in targets:
            if isinstance(target, ast.Name):
                found[target.id] = value.value
    return found


def test_the_repository_module_defines_no_query_language() -> None:
    offenders = sorted(_module_level_query_literals(repository))

    assert not offenders, f"{len(offenders)} query literals still declared in repository.py — they belong in cypher.py / postgres.py: {offenders[:8]}"


def test_the_two_dialects_live_in_two_modules() -> None:
    """Cypher and SQL are not one concern with two spellings: the graph is AGE-over-Postgres and the event
    feed is plain relational Postgres, with different failure modes, different bootstrap and different
    bounds. Splitting them into one 'queries' bag would keep exactly the confusion this finding names."""
    cypher_queries = _module_level_query_literals(cypher)
    sql_queries = _module_level_query_literals(postgres)

    assert cypher_queries and sql_queries
    assert not [name for name, q in cypher_queries.items() if "public.lineage_" in q], "raw table SQL leaked into the Cypher DSL"
    assert not [name for name, q in sql_queries.items() if "MERGE (" in q], "Cypher leaked into the event-feed SQL"
