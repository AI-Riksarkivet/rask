# rahcp-tracker

Standalone package for pluggable transfer state tracking. Used by both `rahcp-client` (S3 bulk transfers) and `rahcp-iiif` (IIIF downloads).

```bash
uv pip install rahcp-tracker
```

| Class | Description |
|-------|-------------|
| `TrackerProtocol` | Interface — 8 methods: `mark()`, `commit()`, `close()`, `summary()`, `done_keys()`, `error_entries()`, `unverified_keys()`, `unvalidated_keys()` |
| `SqliteTracker` | Default SQLite implementation with WAL mode and buffered writes |
| `TransferTracker` | Backwards-compatible alias for `SqliteTracker` |
| `Transfer` | SQLModel table definition — shared across SQLite and Postgres |
| `TransferStatus` | Enum: `pending`, `done`, `error` |

```python
from rahcp_tracker import SqliteTracker, TransferStatus
tracker = SqliteTracker(Path("my-job.db"))
tracker.mark("file.jpg", 12345, TransferStatus.done, etag='"abc"', validated=True)
print(tracker.summary())  # {"pending": 0, "done": 1, "error": 0}
tracker.close()
```

The `Transfer` model in `models.py` tracks: `key`, `size`, `status`, `error`, `etag`, `validated`, `verified`, `updated_at`.
