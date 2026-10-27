"""Who a verified caller is: `<idp-id>~<subject>`, the one key every grant, inbox and attribution names ([[LH-063]]).

ADR 0159 D5 and ADR 0156 C5. The idp id names the identity provider that vouched for the caller, so two providers'
subjects never share a namespace, and renaming a provider in its own configuration re-keys nobody. `kubernetes` is the
cluster's service-account issuer, whose subject is the account's username (`system:serviceaccount:<ns>:<sa>`);
`dex-client` is a Dex client signed in for itself with the client-credentials grant, whose subject is its client id
(ADR 0181); every other idp id is one the deployment names for a Dex connector (`RASK_OIDC_CONNECTORS`), whose subject
is the upstream user id Dex reports for it (`service_kit.governed.idp_identity`). The first two are machines. The idp
id's grammar excludes `~`, so the first `~` ends it and the subject may carry any other character (Lakekeeper's `UserId`,
crates/lakekeeper/src/service/authn.rs).

A string that names no identity provider is no principal. Every place that requires one parses it here: a grant, a
seed, an admin list, an inbox address. A bare name is refused there, because no token can ever prove it.
"""

from __future__ import annotations

import re
import unicodedata
from typing import Annotated, Final, Self
from urllib.parse import quote

from pydantic import AfterValidator, BaseModel, ConfigDict, model_validator


SEPARATOR: Final = "~"
#: The idp id of the cluster's service-account issuer. No Dex connector may be given it.
SERVICE_ACCOUNT_IDP: Final = "kubernetes"
#: The idp id of a Dex client signed in with the client-credentials grant. No Dex connector may be given it, so a client
#: id and a person's user id never name one principal.
CLIENT_IDP: Final = "dex-client"
#: The idp ids whose principals are machines: a person is a principal under any other.
MACHINE_IDPS: Final = frozenset({SERVICE_ACCOUNT_IDP, CLIENT_IDP})
#: Lakekeeper's idp-id grammar (crates/lakekeeper/src/config.rs): it holds no `~`, so the first `~` ends it.
_IDP_ID: Final = re.compile(r"[a-z0-9-]+")
#: OpenFGA's ceiling on a tuple's `user` (`TupleKey.user`, `max_bytes: 512` in openfga.proto, v1.18.3). A principal
#: whose `user` would not fit is refused when it forms, so it is never authenticated and then refused at its first check.
FGA_USER_MAX_BYTES: Final = 512


def require_idp_id(idp_id: str) -> str:
    """`idp_id`, when a principal can carry it. `ValueError` for one outside lower-case letters, digits and `-`."""
    if not _IDP_ID.fullmatch(idp_id):
        raise ValueError(f"the idp id {idp_id!r} is not lower-case letters, digits and '-'")
    return idp_id


class Principal(BaseModel):
    """An identity provider's id and the subject it vouched for."""

    model_config = ConfigDict(frozen=True)

    idp_id: Annotated[str, AfterValidator(require_idp_id)]
    subject: str

    @model_validator(mode="after")
    def _well_formed(self) -> Self:
        if not self.subject:
            raise ValueError("a principal's subject is empty")
        if any(unicodedata.category(char) == "Cc" for char in self.subject):
            raise ValueError("a principal's subject carries a control character")
        if len(self.fga_user) > FGA_USER_MAX_BYTES:
            raise ValueError(f"a principal's OpenFGA user is longer than {FGA_USER_MAX_BYTES} bytes")
        return self

    @classmethod
    def parse(cls, key: str) -> Principal:
        """The principal `key` names. `ValueError` for a key naming no identity provider, or one that is not well formed."""
        idp_id, separator, subject = key.partition(SEPARATOR)
        if not separator:
            raise ValueError(f"{key!r} names no identity provider: a principal is '<idp-id>{SEPARATOR}<subject>'")
        return cls(idp_id=idp_id, subject=subject)

    @property
    def key(self) -> str:
        """`<idp-id>~<subject>`: the subject id the estate keys grants, inboxes and attribution on."""
        return f"{self.idp_id}{SEPARATOR}{self.subject}"

    @property
    def fga_user(self) -> str:
        """The OpenFGA user this principal is: `user:` and the key, percent-encoded.

        Everything outside `A-Za-z0-9_.-~` is encoded, as Lakekeeper encodes a `UserId` for OpenFGA
        (crates/authz-openfga/src/entities.rs). OpenFGA takes a user id with one `:` and no `#` as one user (a second
        `:` is malformed, measured on the live store 2026-10-14; a `#` makes it a userset), and refuses whitespace, so a
        subject carrying any of them is stored as the one user it is.
        """
        return f"user:{quote(self.key, safe='')}"

    @property
    def is_service(self) -> bool:
        """Whether the cluster's service-account issuer vouched for it: a service account, whose signing key it names."""
        return self.idp_id == SERVICE_ACCOUNT_IDP

    @property
    def is_machine(self) -> bool:
        """Whether a machine identity vouched for it, a service account or a Dex client: the estate's one test of a machine."""
        return self.idp_id in MACHINE_IDPS


def is_principal(value: str) -> bool:
    """Whether `value` is a principal key: a well-formed `<idp-id>~<subject>`."""
    try:
        Principal.parse(value)
    except ValueError:
        return False
    return True


def _principal_key(key: str) -> str:
    return Principal.parse(key).key


#: A setting or a field that must name a principal: a bare name is refused when it is read.
type PrincipalKey = Annotated[str, AfterValidator(_principal_key)]
