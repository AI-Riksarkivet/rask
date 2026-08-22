"""A published project reached its publisher's inbox as the SERVICE, and reached them not at all.

The annotator publishes on a person's behalf using a service identity — a pinned token, or one minted
fresh from the IdP for the run. The catalog's `enforce_author` then overwrites the author facet with
that service's verified sub, which is "never trust the request body" doing its job. The consequence is
that the human who clicked publish is absent from every event the publish produces, so a publish that
fails at the catalog tells them nothing.

`lance.originator` is the field for exactly this shape — a run authored by a service, run FOR a person.
Carrying the sub through our own call graph is the only fix; nothing downstream can recover it.

Both transports must carry it. The publish is TWO calls — a direct-HTTP create (the generated client
cannot send the S4 source pins) and an SDK tag — and they set headers by different mechanisms. Wiring
one leaves the other anonymous, which is the partial-threading shape that looks fixed.
"""

from __future__ import annotations

import inspect

from annotator.projects.lakehouse import CatalogPublisher


class TestThePublisherTakesARequester:
    def test_it_accepts_an_originator(self) -> None:
        assert "originator" in inspect.signature(CatalogPublisher.__init__).parameters

    def test_it_is_keyword_only(self) -> None:
        """A positional would sit next to `token` — an identity and a credential are not
        interchangeable and must not be swappable by argument order."""
        param = inspect.signature(CatalogPublisher.__init__).parameters["originator"]
        assert param.kind is inspect.Parameter.KEYWORD_ONLY


class TestBothTransportsCarryIt:
    def _publisher(self, originator: str | None) -> CatalogPublisher:
        return CatalogPublisher(
            "http://catalog:8000",
            token="service-token",
            originator=originator,
            data_api=object(),
            tag_api=object(),
        )

    def test_the_http_create_sends_the_header(self) -> None:
        headers = self._publisher("alice")._headers
        assert headers.get("x-lance-originator") == "alice"

    def test_no_requester_sends_no_header(self) -> None:
        """A service-driven publish with nobody behind it must stay unattributed rather than send an
        empty claim the catalog would have to decide about."""
        assert "x-lance-originator" not in self._publisher(None)._headers

    def test_the_sdk_client_gets_it_too(self) -> None:
        """The tag call goes through the generated client, which reads its own default headers."""
        source = inspect.getsource(CatalogPublisher.__init__)
        assert source.count("x-lance-originator") >= 2, "only one of the two transports carries it"


class TestTheSagaPassesTheActor:
    def test_the_publish_saga_names_the_subject_it_already_has(self) -> None:
        from annotator.projects import lakehouse

        source = inspect.getsource(lakehouse)
        _, _, tail = source.partition("publisher = CatalogPublisher(")
        assert tail, "the publisher construction moved — this test is asserting on nothing"
        assert "originator=subject" in tail.split(")")[0]
