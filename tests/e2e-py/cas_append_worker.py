"""The append worker for the CAS suite, in a module a SPAWNED child can actually import.

It cannot live in the test module. Under the repo's `--import-mode=importlib`, pytest names a suite
from its rootdir-relative path — `tests.e2e-py.test_object_store_cas_e2e` — and that name is not
importable by anything: `tests` is a namespace directory and `e2e-py` is not a legal identifier. A
spawned child unpickling the work item therefore died `ModuleNotFoundError: No module named 'tests'`,
which reads as a missing dependency rather than a missing path.

A flat sibling module fixes it at the root: the suite puts this directory on `PYTHONPATH` before
creating the pool, so the child imports `cas_append_worker` by a name that resolves.

Fork would sidestep the whole problem and must not be used — lance holds its own threads and a forked
child can deadlock, which is why the suite spawns.
"""

from __future__ import annotations


def append_rows(args: tuple[str, dict[str, str], int, int]) -> None:
    """A worker PROCESS: append ``n`` rows starting at ``start`` to the Lance dataset at ``uri``. Runs in its
    own interpreter (ProcessPoolExecutor), so the appends genuinely contend on the manifest commit."""
    uri, so, start, n = args
    import lance as _lance
    import pyarrow as _pa

    _lance.write_dataset(_pa.table({"id": list(range(start, start + n))}), uri, mode="append", storage_options=so)
