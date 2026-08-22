"""`ItemSource.where` is the MEDIA-REGISTRY id, and catalog-qualifying it would break every send.

`docs/architecture/ingest-and-tier-movement.md` §3 FIX 3 proposed carrying the send's dataset as a namespace-qualified
catalog id (`bind86-bronze$pages`) so `source_pin` resolves and the publish's CREATE event gains a
version-pinned DERIVED_FROM input. The payoff is real — today `source_pin` returns None on every send
(it requires the delimiter), so the graph cannot answer "which corpus produced these labels".

The same section filed the prerequisite as its open question 4: *"Can `ItemSource.where` actually be
made catalog-qualified? It is currently resolved through the media dataset registry, keyed on BARE
dataset ids — not catalog table ids."*

IT CANNOT, and this test is the answer. `_refuse_unknown_datasets` resolves every `where` through
`dataset_handle(state, name)` against `state.registry`, whose ids are bare. Emitting
`bind86-bronze$pages` from the senders — exactly what FIX 3 says to do — makes that lookup miss, and
the guard refuses THE WHOLE SEND with "dataset(s) [...] do not exist". So the change that was supposed
to enrich lineage would instead make it impossible to send anything into a project at all, and it
would fail at the door with a message pointing at the wrong problem.

That guard is not incidental. It exists because an item naming an unresolvable dataset can never be
claimed, submitted or skipped, and the publish precondition requires every task terminal — one such
item wedges the project permanently.

So the pin has to be resolved SERVER-SIDE, from the registry id to the catalog id, or carried as a
SECOND field beside `where`. Which of those is a design decision this test does not make. What it does
is stop the wrong one being applied by someone reading FIX 3 as an instruction.
"""

from __future__ import annotations

import inspect

from annotator.api.v1.endpoints import project_events
from annotator.projects import publish


class TestTheTwoIdSpacesAreDistinct:
    def test_the_send_guard_resolves_where_through_the_MEDIA_registry(self) -> None:
        source = inspect.getsource(project_events._refuse_unknown_datasets)
        assert "dataset_handle(state, name)" in source, (
            "the guard no longer resolves through the media registry — re-check whether `where` is still that registry's key before qualifying it"
        )
        assert "state.registry.list_ids()" in source

    def test_source_pin_requires_a_CATALOG_id_and_returns_None_otherwise(self) -> None:
        """The two halves of the mismatch, in one assertion: the guard wants a bare id, the pin wants
        a delimited one, and one field cannot be both."""
        source = inspect.getsource(publish.source_pin)
        assert "delimiter" in source
        assert "return None" in source, "source_pin no longer degrades on an unqualified id"

    def test_the_refusal_is_deliberate_and_was_measured(self) -> None:
        """Not an oversight to be fixed by qualifying the name — the OPPOSITE change was tried and
        broke the publish. `source_pin`'s own docstring records it: sending the bare media name made
        the catalog authorize `table:transcripts_v2`, an object that does not exist, "and FGA denies
        before it checks existence, so the ENTIRE publish failed ... Observed live, 2026-08-03."
        """
        doc = inspect.getdoc(publish.source_pin) or ""
        assert "Observed live, 2026-08-03" in doc, (
            "the measurement that justifies this guard is gone from the docstring — do not relax the guard without re-establishing it"
        )
        assert "no catalog node at the other end" in " ".join(doc.split())

    def test_the_guard_clause_is_still_in_the_body(self) -> None:
        source = inspect.getsource(publish.source_pin)
        assert "if delimiter not in dataset:" in source
        assert "return None" in source


class TestTheSendersStayInTheRegISTRYIdSpace:
    """A guard against the tempting one-line "fix". Both senders emit `where` from the corpus the user
    picked, which is a media-registry id; changing either to emit a catalog id refuses the send."""

    def test_the_annotator_sender_emits_the_bare_dataset(self) -> None:
        from pathlib import Path

        src = Path(__file__).resolve().parents[3] / "frontend/microfrontends/annotator/src/lib/select/bulk-send.ts"
        assert src.exists(), src
        assert "where: dataset" in src.read_text(), (
            "bulk-send.ts no longer emits the registry id — if it now emits a catalog-qualified one, `_refuse_unknown_datasets` will refuse every send"
        )

    def test_the_explorer_sender_does_too(self) -> None:
        from pathlib import Path

        src = Path(__file__).resolve().parents[3] / "frontend/microfrontends/explorer/src/lib/components/SendToProjectDialog.svelte"
        assert src.exists(), src
        assert "where: dataset" in src.read_text()
