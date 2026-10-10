# 0081. `dropped_at` means "no longer catalogued at this id" (LH-144, owner scope 2026-09-27)

The catalog announces a drop as a `DatasetEvent`, which leaves no run, and `repository.dropped_at` read
only run history, so every catalog drop read as a live table whose grants were gone: the reconcile named
it `ungoverned` on every tick (measured 2026-09-27: `ungoverned=83`, including tables created and dropped
through the catalog that day). A drop is now the newer of two facts, a tie going to existence: a
lifecycle stamp on the Dataset node, written by every CREATE or DROP fact (the spec's
`lifecycleStateChange` facet first, `lance.operation` otherwise) and only by one newer than the stamp it
replaces; and the newest COMPLETE run that wrote the dataset, maintenance excluded. Run history stays a
source because it holds every drop the graph recorded before the stamp existed (955 datasets on the live
graph) and because a data write after a drop proves the table is back.

Two readings change with it. A DEREGISTER now makes `dropped_at` fire, where the rename entry above
chose DEREGISTER so it would not: the question `dropped_at` answers is whether the id is still
catalogued, and a rename's source id is not; the bytes are expected under the destination id, whose
REGISTER marker carries the location. And an undrop emits a `register_table` marker per table, because
without one an undropped table stayed dropped for the sweep until something wrote it.

The reconcile records a drop nobody announced (owner D14(4)): an ungoverned dataset whose bytes are
cleanly gone at the location it read gets a DROP stamp and a feed row, never on a missing bucket or a
denied read, which stay `ungoverned` with their reason; one whose bytes are present is `ungoverned_live`.
