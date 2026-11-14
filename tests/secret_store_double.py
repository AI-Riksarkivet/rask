"""The secret store's HTTP API as the offline suite answers it, for the root conftest's `secret_store` fixture.

A plain module, so a test names the double's type and its address in one place instead of writing either out.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Final

import httpx
import respx


class SecretStoreDouble:
    """A login the store admits, a token it honours, and a read of each secret a test has it hold. A read of a secret it
    holds none of is not mocked, and fails the test that made it.

    The shapes are the store's own, measured on the development OpenBao from inside the catalog's container as the
    catalog's ServiceAccount (2026-11-13 and 2026-11-14): `POST /v1/auth/<mount>/login` with the role and the projected
    token answers `auth.client_token` and `auth.lease_duration`; `GET /v1/<kv mount>/data/<name>` under `X-Vault-Token`
    answers the fields at `data.data`; `GET /v1/auth/token/lookup-self` answers 200 to a token the store honours and 403
    to one it never issued; a secret the policy does not name, a token the store never issued and a role's wrong account
    answer 403, and a role the store does not hold answers 400.
    """

    ADDRESS: Final = "http://secret-store.test:8200"
    ROLE: Final = "rask-test"
    LOGIN: Final = f"{ADDRESS}/v1/auth/kubernetes/login"
    #: Where a token asks the store about itself.
    LOOKUP: Final = f"{ADDRESS}/v1/auth/token/lookup-self"
    #: Where a secret is read: ``f"{SECRETS}/<name>"``.
    SECRETS: Final = f"{ADDRESS}/v1/secret/data"
    #: The store token the double's login issues.
    TOKEN: Final = "s.offline-suite"  # noqa: S105 — a test value no store issued

    def __init__(self) -> None:
        # Its own router, beside whatever a test routes on respx's global one: a request is tried against every started
        # router, so the store answers here while a test's other doubles answer there. The router does not ask that
        # every route was called, since the login is one and a test may read nothing: the fixture asks it of the secrets.
        self.router: Final = respx.MockRouter(assert_all_called=False)
        #: The login, which admits every call; a test replaces its answers to refuse or to count them.
        self.login: Final = self.router.post(self.LOGIN).mock(return_value=self.logged_in())
        #: The store's answer to a token asking about itself, which honours every one; a test replaces its answers to
        #: have the store drop a token, or to count the questions.
        self.lookup: Final = self.router.get(self.LOOKUP).mock(return_value=self.honoured())
        self._held: dict[str, respx.Route] = {}

    def hold(self, name: str, fields: Mapping[str, str] | httpx.Response) -> respx.Route:
        """Answer every read of secret ``name`` from now on with ``fields``, or with a response of the test's own.

        Returns:
            The read's route, whose calls a test counts and whose answers it may replace.
        """
        route = self.router.get(f"{self.SECRETS}/{name}").mock(return_value=fields if isinstance(fields, httpx.Response) else self.held(fields))
        self._held[name] = route
        return route

    def unread(self) -> list[str]:
        """The secrets a test had the store hold that nothing read."""
        return sorted(name for name, route in self._held.items() if not route.called)

    @classmethod
    def logged_in(cls, *, token: str = TOKEN, lease_seconds: int = 900) -> httpx.Response:
        """The store's answer to a login it admits."""
        return httpx.Response(200, json={"auth": {"client_token": token, "lease_duration": lease_seconds, "renewable": True}})

    @staticmethod
    def honoured() -> httpx.Response:
        """The store's answer to a token it honours asking about itself."""
        return httpx.Response(200, json={"data": {"display_name": "kubernetes-default-rask-sa-test", "ttl": 899}})

    @staticmethod
    def held(fields: Mapping[str, str]) -> httpx.Response:
        """The store's answer to a read of a secret holding ``fields``."""
        return httpx.Response(200, json={"data": {"data": dict(fields), "metadata": {"version": 1}}})

    @staticmethod
    def refused(status: int = 403, reason: str = "permission denied") -> httpx.Response:
        """The store's refusal: 403 for what a token may not read or a login it does not admit, 400 for a role it does not hold."""
        return httpx.Response(status, json={"errors": [reason]})
