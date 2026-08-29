"""SK-19 — the shared media resolution entry point returned an implicit `Any`.

`dataset_handle` is the funnel every media and annotator route passes through to reach a dataset. It
carried no return annotation, so the handle each of those routes holds was `Any` from its first line:
`handle.dbb` type-checks, and renaming an attribute on `DatasetHandle` reddens nothing anywhere in
the fleet. It survived lint because the whole `media/**` subtree is ANN-exempt in `pyproject.toml`.

`SlashToleranceMiddleware`'s `routes_provider` had the same shape of hole in its parameter — a bare
`list` with no element type — on the one middleware `make_service_app` installs for every app.
"""

from __future__ import annotations

import inspect
from collections.abc import Sequence

from starlette.routing import BaseRoute

from service_kit.media.state import dataset_handle
from service_kit.slash import SlashToleranceMiddleware


def test_dataset_handle_declares_the_handle_it_returns() -> None:
    annotation = inspect.signature(dataset_handle).return_annotation
    assert annotation is not inspect.Signature.empty, "the fleet's media routes all start from an untyped Any"
    assert annotation == "DatasetHandle"


def test_the_declared_type_is_the_one_the_registry_actually_hands_back() -> None:
    """A string annotation is only as good as the name it resolves to."""
    from service_kit.lancekit.registry import DatasetHandle, DatasetRegistry
    from service_kit.media import state

    resolved = inspect.get_annotations(dataset_handle, eval_str=True, globals={**vars(state), "DatasetHandle": DatasetHandle})["return"]
    assert resolved is DatasetHandle
    assert inspect.signature(DatasetRegistry.get).return_annotation in {DatasetHandle, "DatasetHandle"}


def test_the_slash_middleware_declares_what_its_provider_yields() -> None:
    annotation = inspect.get_annotations(SlashToleranceMiddleware.__init__, eval_str=True)["routes_provider"]
    assert annotation.__args__[-1] == Sequence[BaseRoute], f"still a bare container: {annotation}"
