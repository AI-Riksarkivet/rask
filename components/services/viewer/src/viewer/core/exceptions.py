"""Moved to service_kit.exceptions. Re-exported for back-compat."""

from service_kit.exceptions import *  # noqa: F403
from service_kit.exceptions import (  # noqa: F401
    DomainError,
    NotFoundError,
    ServiceUnavailableError,
    ValidationError,
    register_handlers,
)
