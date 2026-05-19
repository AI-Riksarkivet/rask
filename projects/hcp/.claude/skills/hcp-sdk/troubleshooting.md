# Troubleshooting & Debugging

## Debugging with log levels

```bash
# See every HTTP request with timing
rahcp --log-level debug s3 ls

# See auth events and summaries
rahcp --log-level info s3 upload-all my-bucket ./data

# In Python
import logging
logging.basicConfig(level=logging.DEBUG)
```

| Level | What you see |
|-------|-------------|
| `debug` | Every HTTP request: method, path, status, duration (ms) |
| `info` | Authentication, upload/download summaries |
| `warning` | Retries, transport errors (default) |
| `error` | Non-retryable failures |

## Common errors and fixes

### `AuthenticationError: Login failed: 401`

**Cause:** Wrong username/password/tenant, or API server not running.

**Fix:**
```bash
# Test auth directly
curl -s http://localhost:8000/api/v1/auth/token \
  -d "username=admin&password=secret&tenant=dev-ai"

# Check config is being loaded
rahcp --log-level debug --config .rahcp/config.yaml auth whoami
```

**Most common cause:** CLI defaults to `~/.rahcp/config.yaml`. If your config is at `.rahcp/config.yaml` (project-local), you MUST pass `--config .rahcp/config.yaml` on every command. Without it, the CLI has no credentials and every request returns 401.

### `AuthenticationError` on first request (not login)

**Cause:** Client created without `async with` — login never happened.

```python
# WRONG — no login
client = HCPClient.from_env()
await client.s3.list_buckets()  # 401!

# RIGHT — async with triggers login
async with HCPClient.from_env() as client:
    await client.s3.list_buckets()  # works
```

### `NotFoundError` on head/download

**Cause:** Object doesn't exist, or wrong bucket/key.

```bash
# Verify the object exists
rahcp s3 ls my-bucket --prefix path/to/

# Check exact key (case-sensitive)
rahcp s3 ls my-bucket -f filename
```

### Uploads succeed but tracker shows errors

**Cause:** Pre-upload validation failed (not a network error).

```python
# Check error messages in tracker
from rahcp_tracker import SqliteTracker
t = SqliteTracker(Path(".rahcp/.upload-tracker.db"))
for key, size in t.error_entries():
    print(key)
t.close()
```

```bash
# Or query the SQLite directly
sqlite3 .rahcp/.upload-tracker.db \
  "SELECT key, error FROM transfer WHERE status='error' LIMIT 10"
```

Validation errors start with `validation:` (e.g. `validation: Missing JPEG EOI marker`). These are corrupt source files, not upload failures.

### Bulk transfer hangs or is slow

**Diagnose:**
```bash
# Check if API is responsive
curl -s -w "%{time_total}s\n" http://localhost:8000/api/v1/health

# Check tracker progress from another terminal
sqlite3 .rahcp/.upload-tracker.db \
  "SELECT status, COUNT(*) FROM transfer GROUP BY status"
```

**Common causes:**

| Symptom | Cause | Fix |
|---------|-------|-----|
| 0 files/s | API down or unreachable | Check endpoint URL, `verify_ssl` |
| Low files/s, high errors | Rate limiting or timeouts | Reduce `--workers`, increase `timeout` |
| High files/s but plateaus | Network saturated | Normal — don't increase workers |
| Hangs completely | DNS or SSL handshake | Try `verify_ssl: false` for local dev |

### IIIF download: connection timeout

**Cause:** IIIF server unreachable from current machine (firewall, VPN, DNS).

```bash
# Test connectivity
curl -v -m 10 "https://iiifintern-ai.ra.se/arkis!C0074667/manifest"

# Try with custom URL
rahcp iiif download C0074667 --iiif-url https://other-iiif-server.example.com
```

### IIIF download: empty manifest (0 images)

**Cause:** Invalid batch ID, or IIIF server returns empty `items` array.

```bash
# Check manifest manually
curl -s "https://iiifintern-ai.ra.se/arkis!C0074667/manifest" | python3 -c "
import sys, json
data = json.load(sys.stdin)
print(f'Items: {len(data.get(\"items\", []))}')
"
```

### `--validate` flag: `rahcp-validate not installed`

```bash
# Install validation support
uv pip install "rahcp-cli[validate]"
# or
uv pip install "rahcp[validate]"
```

### Wrong prefix / flat structure

Uploads preserve local directory structure as S3 key prefixes. `--prefix` prepends to all keys.

```bash
# Source: ./scans/batch1/image.jpg
rahcp s3 upload-all bucket ./scans
# → s3://bucket/batch1/image.jpg  (flat relative to source dir)

rahcp s3 upload-all bucket ./scans --prefix data/
# → s3://bucket/data/batch1/image.jpg  (prefixed)
```

To fix wrong keys: delete and re-upload. Use `list_objects` + `delete_bulk` to clean up.

## Tracker inspection

```bash
# Summary
sqlite3 .rahcp/.upload-tracker.db \
  "SELECT status, COUNT(*) FROM transfer GROUP BY status"

# Recent uploads
sqlite3 .rahcp/.upload-tracker.db \
  "SELECT key, updated_at FROM transfer WHERE status='done' ORDER BY updated_at DESC LIMIT 5"

# All errors with reasons
sqlite3 .rahcp/.upload-tracker.db \
  "SELECT key, error FROM transfer WHERE status='error'"

# Retry only errors
rahcp s3 upload-all my-bucket ./data --retry-errors
```

## Performance tuning

| Scenario | Recommended settings |
|----------|---------------------|
| Many small files (< 5 MB) | `bulk_workers: 60`, `bulk_presign_batch_size: 500`, `bulk_queue_depth: 16` |
| Few large files (> 100 MB) | `bulk_workers: 10`, `multipart_concurrency: 10`, `bulk_chunk_size: 4194304` |
| Slow network | `bulk_workers: 5`, `timeout: 120`, `bulk_presign_batch_size: 100` |
| Fast datacenter (> 1 Gbps) | `bulk_workers: 60`, `bulk_presign_batch_size: 500`, `bulk_queue_depth: 16`, `bulk_tracker_flush_every: 1000` |

All tunable via config file or CLI flags. `--workers 0` uses the config value. See `rahcp s3 upload-all --help` for all options.

## Running long transfers safely

```bash
# Always use tmux/screen for long transfers
tmux new -s upload
rahcp s3 upload-all my-bucket ./data --workers 20
# Ctrl+B, D to detach — reattach: tmux attach -t upload
```

If interrupted, re-run the same command. The tracker skips completed files instantly.

## Post-transfer verification

```bash
# Verify all local files exist in bucket with matching sizes
rahcp s3 verify my-bucket ./scans --prefix data/

# Output shows OK, MISSING, SIZE_MISMATCH
# Exits with code 1 if any issues — usable in scripts
```
