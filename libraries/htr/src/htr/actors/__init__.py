"""Ray Data actor classes — one per pipeline stage. Designed to be passed to
`ds.map_batches(Cls, ...)`.

Each actor:
  - takes config-only `__init__` (cheap-to-pickle)
  - loads its model on first call (Ray creates one actor instance per replica)
  - exposes `__call__(batch: dict[str, np.ndarray]) -> dict[str, np.ndarray]`
  - reads/writes `dict[column_name -> np.ndarray]` where rows are pages

Columns flowing through the pipeline (one row per page):
  - `key:           str`              (input — image key/path)
  - `image_bytes:   bytes`            (raw JPEG)
  - `regions:       list[Region]`     (after LayoutActor)
  - `lines:         list[Line]`       (after LineActor; flat list across regions)
  - `transcribed:   list[TranscribedLine]`  (after TranscribeActor)
  - `output_key:    str`              (after AltoExportActor)
  - `alto_xml:      bytes`            (after AltoExportActor)
"""
