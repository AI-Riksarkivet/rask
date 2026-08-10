"""The wire-name translation, the id derivation, and the sweep that keeps both mandatory.

Dapr's `ActorProxy.__getattr__` dispatches from a map keyed by the WIRE name (`@actormethod(name=
"MarkSeen")`), not the Python attribute. Unit-test fakes implement the Python names, so a plane can be
green at every gate while every real cross-actor call raises `AttributeError` against the sidecar —
which is precisely what the annotator's first live drive found. These tests pin the translation AND
sweep this service's own tree onto it.

The estate-wide sweep in `tests/unit/test_actor_proxy_names.py` walks
`services/annotator/src/annotator` by path, so it does NOT cover this module: the equivalent gate has
to live here until that root is widened.
"""

import ast
import pathlib
import string
from typing import Any

import pytest

from notifications.errors import InboxUnreadable
from notifications.inbox_actor import INBOX_ACTOR_TYPE, InboxActor, InboxActorInterface
from notifications.proxies import TypedActorProxy, inbox_actor_id
from service_kit.governed.user_state import DAPR_APP_ID_SEPARATOR, decode_subject


SRC = pathlib.Path(__file__).resolve().parents[1] / "src/notifications"


class _WireOnlyProxy:
    """Behaves like the real `ActorProxy`: ONLY wire names resolve."""

    def __init__(self, wire_names: set[str], raises: Exception | None = None) -> None:
        self._wire = wire_names
        self._raises = raises
        self.calls: list[str] = []

    def __getattr__(self, name: str) -> Any:
        if name not in self._wire:
            raise AttributeError(f"has no attribute {name}")

        async def _call(*args: Any) -> dict[str, Any]:
            self.calls.append(name)
            if self._raises is not None:
                raise self._raises
            return {}

        return _call


def _wire_names(interface: type) -> set[str]:
    return {v.__actormethod__ for v in interface.__dict__.values() if hasattr(v, "__actormethod__")}


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("python_name", "wire_name"),
    [("deliver", "Deliver"), ("page", "Page"), ("unread", "Unread"), ("mark_seen", "MarkSeen"), ("dismiss", "Dismiss")],
)
async def test_python_names_reach_the_wire_names(python_name: str, wire_name: str) -> None:
    raw = _WireOnlyProxy(_wire_names(InboxActorInterface))
    proxy = TypedActorProxy(raw, InboxActorInterface)

    await getattr(proxy, python_name)({})

    assert raw.calls == [wire_name]


def test_every_interface_method_declares_an_explicit_wire_name() -> None:
    """A bare `@actormethod()` stores `None`, and the proxy then computes a null wire name — a routing
    id nothing answers to, produced silently."""
    declared = [v for v in InboxActorInterface.__dict__.values() if hasattr(v, "__actormethod__")]
    assert declared, "the interface declares no actor methods"
    assert all(isinstance(v.__actormethod__, str) and v.__actormethod__ for v in declared)


def test_an_undeclared_method_fails_loudly() -> None:
    proxy = TypedActorProxy(_WireOnlyProxy(set()), InboxActorInterface)
    with pytest.raises(AttributeError, match="declares no method"):
        _ = proxy.not_a_method


@pytest.mark.asyncio
async def test_an_unreadable_inbox_survives_the_sidecar_hop_as_itself() -> None:
    """Dapr serialises an actor's exception into a 500 body, so without the translation every
    `except InboxUnreadable` in the routes stops matching the moment the check runs inside an actor —
    and a record this service deliberately refuses reaches the browser as a bare 500."""
    raw = _WireOnlyProxy(_wire_names(InboxActorInterface), raises=RuntimeError("... InboxUnreadable: inbox-rows ..."))
    proxy = TypedActorProxy(raw, InboxActorInterface)

    with pytest.raises(InboxUnreadable):
        await proxy.unread()


@pytest.mark.asyncio
async def test_any_other_actor_failure_is_re_raised_untouched() -> None:
    raw = _WireOnlyProxy(_wire_names(InboxActorInterface), raises=RuntimeError("connection refused"))
    proxy = TypedActorProxy(raw, InboxActorInterface)

    with pytest.raises(RuntimeError, match="connection refused"):
        await proxy.unread()


def test_the_actor_type_constant_matches_the_class_the_runtime_registers() -> None:
    """Dapr derives the actor type from the class name; the proxy reads the constant. If they drift,
    every call addresses an actor type nothing hosts."""
    assert InboxActor.__name__ == INBOX_ACTOR_TYPE


#: Subject shapes an identity provider really emits, each carrying a character the id has to survive:
#: a Dex opaque handle, Auth0's `|` (and the DOUBLED form, which is Dapr's reserved app-id separator), a
#: Keycloak uuid, an email `sub`, the three characters standard base64 would introduce (`+`, `/`, `=`),
#: a non-ASCII name, and a long one. One hand-picked example is least likely to contain exactly the
#: character that breaks a key.
_SUBJECTS = [
    "CgVhbGljZRIFbG9jYWw",
    "auth0|abc/def+ghi==",
    "a||b",
    "8e2f5f4c-1e2a-4b6d-9f3e-2c1d0b7a6e55",
    "alice@example.test",
    "Åsa Öberg",
    "x" * 512,
]
_SUBJECT_IDS = ["dex-handle", "auth0-pipe", "doubled-separator", "keycloak-uuid", "email", "non-ascii", "very-long"]


@pytest.mark.parametrize("subject", _SUBJECTS, ids=_SUBJECT_IDS)
def test_the_actor_id_round_trips_to_the_subject(subject: str) -> None:
    """Reversible on purpose: an operator has to be able to read a Postgres row back to a person."""
    assert decode_subject(inbox_actor_id(subject)) == subject


@pytest.mark.parametrize("subject", _SUBJECTS, ids=_SUBJECT_IDS)
def test_the_actor_id_is_safe_as_written_in_the_sidecar_url_path(subject: str) -> None:
    """The id travels in the PATH of the sidecar's actor API, which decodes on arrival — so
    percent-encoding is not available (a `%7C%7C` would arrive as `||` again) and the id has to be safe
    unescaped. base64url's alphabet with the padding stripped is exactly that: no `|`, `/`, `+`, `%`
    or `=`. The `||` guard in `inbox_actor_id` is what must stay true if the encoding is ever revisited;
    this is the property that makes it currently unreachable."""
    actor_id = inbox_actor_id(subject)
    assert set(actor_id) <= set(string.ascii_letters + string.digits + "-_")
    assert DAPR_APP_ID_SEPARATOR not in actor_id


@pytest.mark.parametrize("subject", ["", " ", "\t\n"], ids=["empty", "one-space", "whitespace"])
def test_an_empty_subject_has_no_inbox(subject: str) -> None:
    """There is no anonymous inbox. Refused rather than encoded, because base64url of `""` is `""` — an
    actor id the sidecar would route somewhere, and every anonymous caller to the same place."""
    with pytest.raises(ValueError, match="non-empty verified subject"):
        inbox_actor_id(subject)


def _callers_of(target: str) -> list[str]:
    """Modules outside `proxies.py` that CALL `target`, read from the AST rather than the text.

    From the AST because a substring sweep matches the name inside a docstring or a comment, which is
    how a gate cries wolf — the estate's declared-dependency gate says the same, and this one proved it
    on its first run: `inbox_actor`'s module docstring explains `encode_subject` and was reported as a
    caller of it.
    """
    offenders = []
    for path in sorted(SRC.rglob("*.py")):
        if path.name == "proxies.py":
            continue
        for node in ast.walk(ast.parse(path.read_text())):
            if not isinstance(node, ast.Call):
                continue
            func = node.func
            called = f"{func.value.id}.{func.attr}" if isinstance(func, ast.Attribute) and isinstance(func.value, ast.Name) else getattr(func, "id", "")
            if called == target:
                offenders.append(str(path.relative_to(SRC)))
                break
    return offenders


def test_no_call_site_builds_a_raw_actor_proxy() -> None:
    """The sweep: every proxy in this service goes through `typed_proxy`. A raw `ActorProxy.create`
    outside proxies.py reintroduces the wire-name mismatch for whatever pythonic call follows it."""
    assert _callers_of("ActorProxy.create") == []


def test_no_module_derives_an_inbox_actor_id_by_hand() -> None:
    """`inbox_actor_id` is the one derivation, and it carries the empty-subject and separator guards.
    A second `encode_subject` call site is a second place those guards can be forgotten."""
    assert _callers_of("encode_subject") == []
