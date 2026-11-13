"""Each credential a live run hands the suites, read from the file the run wrote it to ([[XC-202]]).

A runner (`scripts/e2e_live.sh`, `scripts/e2e_stack.sh`) writes every credential to a file of its own in a directory only
its user reads, and exports nothing but that directory, as `LANCE_E2E_CREDENTIALS`. So no key pair, token or secret
sits in a suite's environment, which every process the run starts inherits and anything that can read the run's
`/proc` entries can list. A credential the run did not supply reads as the caller's default, and the suite that needs
it skips or fails by its own rule.
"""

from __future__ import annotations

import os
from enum import StrEnum
from pathlib import Path


#: The environment variable that names the directory a runner writes the credentials to.
_DIRECTORY_VARIABLE = "LANCE_E2E_CREDENTIALS"


class Credential(StrEnum):
    """A credential a runner may write, by the name of its file."""

    #: A TokenRequest for the suites' own store identity, `rask-e2e-suites`, at the audience the store trusts; a suite
    #: exchanges it at the store's STS for a session under that identity's policy (`correctness.store_options`).
    STORE_TOKEN = "store-token"
    DAPR_TOKEN = "dapr-token"
    #: A bearer of the estate's admin, who administers the scenario's project.
    TOKEN = "token"
    #: The secret of the confidential sign-in client the live lanes' password grant presents.
    DEX_SECRET = "dex-secret"
    #: A `rask-openfga` TokenRequest for the model hook's ServiceAccount; it expires, so its readers re-read the file.
    FGA_TOKEN = "fga-token"
    #: A TokenRequest for lineage's ServiceAccount at the catalog's own door audience.
    SERVICE_TOKEN = "service-token"


def credential_path(name: Credential) -> str | None:
    """The file holding the credential, or None when the run supplied none."""
    directory = os.environ.get(_DIRECTORY_VARIABLE)
    if not directory:
        return None
    path = Path(directory) / name
    return str(path) if path.is_file() else None


def credential(name: Credential, default: str = "") -> str:
    """The credential the run supplied, or `default` when it supplied none."""
    path = credential_path(name)
    return Path(path).read_text(encoding="utf-8").strip() if path else default
