"""Lance's commit-path auto-cleanup: the keys that arm it, and the one way the estate disarms a table ([[LH-245]]).

Lance runs version cleanup INSIDE a commit, every N commits, when the manifest that commit PRODUCES carries
``lance.auto_cleanup.*`` config keys (`lance_docs/guide.md:3857-3923`). Measured on pylance 12.0.0
(2026-10-05, a real dataset on a local root):

* the hook reads the committed manifest's config, not the one committed over: an ``update_config`` that
  arms a table deletes versions in its own commit, a ``delete_config_keys`` that disarms one deletes none,
  and an append through a handle opened BEFORE the disarm rebases onto the disarmed manifest and deletes
  none (``LanceDataset.insert`` and ``LanceDataset.commit`` of an ``Append`` at the stale read version alike);
* ``lance.auto_cleanup.interval`` alone arms it; ``.older_than`` or ``.retain_versions`` alone do not;
* keys in the SCHEMA metadata arm nothing: only the manifest config is read;
* a restore produces the restored version's config, so restoring a version that carried the keys re-arms
  the table and deletes in the restore's own commit;
* an overwrite carries the config forward; tag and branch creation commit nothing on the ref and delete nothing;
* pylance 12's Python ``write_dataset``, ``LanceDataset.insert``, ``merge_insert`` and ``commit`` take no
  ``skip_auto_cleanup`` (the guide names one; the native ``WriteParams`` field exists, but ``write_dataset``'s
  params dict never sets it), so no writer can opt out per call.

That deletion answers to no governance: a legal hold and a protected base are the sweep's gates, the writer
need hold no delete right (Lance then only logs the hook's failure), and nothing records it. So the sweep is
the one reclaimer, and every committer disarms the ref it is about to commit on.
"""

from __future__ import annotations

from collections.abc import Iterable
from typing import TYPE_CHECKING


if TYPE_CHECKING:
    import lance


#: Every manifest config key Lance's commit-path auto-cleanup reads. A PREFIX, not the two keys pylance's
#: ``disable_auto_cleanup`` deletes: measured on 12.0.0, that call removes only ``.interval`` and
#: ``.older_than`` and leaves ``.retain_versions`` standing, a retention instruction the next arming reuses.
AUTO_CLEANUP_PREFIX = "lance.auto_cleanup."


def armed_keys(keys: Iterable[str]) -> list[str]:
    """The ``lance.auto_cleanup.*`` keys among ``keys``, sorted."""
    return sorted(key for key in keys if key.startswith(AUTO_CLEANUP_PREFIX))


def disarm(dataset: lance.LanceDataset) -> list[str]:
    """Delete every ``lance.auto_cleanup.*`` key from the ref ``dataset`` is on, and return the keys deleted.

    WRITES ONLY WHEN A KEY IS PRESENT: ``delete_config_keys`` commits a version even with nothing to delete
    (measured on 12.0.0: a key-free table moved 12 -> 13), and committers call this on every write. The handle
    advances in place to the config-only commit it made (measured on 12.0.0, main and a branch alike), so a
    caller commits its own operation through the same handle next. A failure raises: the caller decides
    whether its own commit may proceed armed.
    """
    armed = armed_keys(dataset.config())
    if armed:
        dataset.delete_config_keys(armed)
    return armed
