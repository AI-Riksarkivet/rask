# rahcp-iiif

Async IIIF image downloader with resumable tracking. Downloads images from Riksarkivet IIIF endpoints in parallel, with optional validation via `rahcp-validate`.

```bash
uv pip install rahcp-iiif
# With validation:
uv pip install "rahcp-iiif[validate]"
```

## Quick start

```python
import asyncio
from pathlib import Path
from rahcp_tracker import SqliteTracker
from rahcp_iiif import download_batch, download_batches

async def main():
    tracker = SqliteTracker(Path(".iiif-download.db"))

    # Download a single batch
    stats = await download_batch(
        "C0074667",
        Path("./images"),
        tracker,
        workers=10,
    )
    print(f"Downloaded {stats.ok}, skipped {stats.skipped}, errors {stats.errors}")

    # Or download multiple batches
    stats = await download_batches(
        ["C0074667", "C0074865", "A0065852"],
        Path("./images"),
        tracker,
        workers=10,
        query_params="full/,1200/0/default.jpg",  # custom resolution
    )
    tracker.close()

asyncio.run(main())
```

## Manifest parsing

```python
from rahcp_iiif import get_image_ids, build_image_url

# Get all image IDs from a IIIF manifest
image_ids = await get_image_ids("C0074667")
# → ["C0074667_00001", "C0074667_00002", ...]

# Build the download URL for a specific image
url = build_image_url("C0074667_00001", query_params="full/,1200/0/default.jpg")
# → "https://iiifintern-ai.ra.se/arkis!C0074667_00001/full/,1200/0/default.jpg"
```

## Configuration

| Setting | Env var | Default | Description |
|---------|---------|---------|-------------|
| `base_url` | `IIIF_URL` | `https://iiifintern-ai.ra.se` | IIIF server base URL |
| `timeout` | `IIIF_TIMEOUT` | `60` | Request timeout in seconds |
| `query_params` | `IIIF_QUERY_PARAMS` | `full/max/0/default.jpg` | IIIF image API parameters |

The `query_params` string follows the [IIIF Image API](https://iiif.io/api/image/3.0/) format: `{region}/{size}/{rotation}/{quality}.{format}`. Common values:

| `query_params` | Description |
|----------------|-------------|
| `full/max/0/default.jpg` | Full resolution JPEG (default) |
| `full/,1200/0/default.jpg` | Scale to 1200px height |
| `full/800,/0/default.jpg` | Scale to 800px width |
| `full/200,200/0/default.jpg` | Fixed 200x200 thumbnail |

## CLI commands

See [CLI — rahcp iiif](cli.md#rahcp-iiif) for command-line usage including `rahcp iiif download` and `rahcp iiif download-batches`.
