"""The spec's refusal for a guard whose read of the object store failed: 503 where the store did not answer, which a
retry may clear, and 500 for an answer about the target, which only a write clears. Which failure is which is the
store's own question (`lakehouse.storage.outage.reads_as_store_outage`)."""

from lance_namespace import InternalError, ServiceUnavailableError

from lakehouse.storage.outage import reads_as_store_outage


__all__ = ["store_refusal"]


def store_refusal(exc: BaseException, *, reading: str, so: str) -> ServiceUnavailableError | InternalError:
    """The spec's refusal for a guard whose ``reading`` failed with ``exc``, saying what it therefore did not do (``so``).

    The store not answering is an outage (503, code 17), which a retry may clear. Anything else, a record that is there
    and cannot be read as one or an answer the store gave, is the target's own state (500, code 18), which only a write
    clears, so it is not told to retry (`lance_docs/ns_catalog/namespace/operations/errors.md`: 17 is "temporarily
    unavailable", 18 an unexpected server error).
    """
    if reads_as_store_outage(exc):
        return ServiceUnavailableError(f"the object store did not answer {reading}, so {so}")
    return InternalError(f"{reading} failed, so {so}")
