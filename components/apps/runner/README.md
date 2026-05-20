# runner

Ray Data batch driver for HTR pipelines built from `htr` actors.

Replaces the legacy `ra-batch` CLI: no YAML, no custom Stage/Runner kernel — just a thin `ray.data.map_batches` chain.

## Usage

```bash
# Local mode (single process, no cluster)
uv run runner --input ./images --output ./alto --pipeline fake

# Against MinIO / HCP
uv run runner \
    --input s3://images-batch \
    --output s3://images-batch-alto \
    --prefix A0060198/ \
    --pipeline htr

# Against a Ray cluster
uv run runner --input ... --output ... \
    --address ray://dev-kuberay.ra.se:10001
```

## Flags

| Flag | Description |
|------|-------------|
| `--input`, `-i` | Filesystem path or `s3://bucket[/prefix]` |
| `--output`, `-o` | Filesystem path or `s3://bucket[/prefix]` |
| `--pipeline` | `htr` (full GPU pipeline) or `fake` (no-GPU smoke test) |
| `--prefix` | Key prefix to scope source listing AND sink resumability |
| `--limit`, `-n` | Process only the first N keys (after the diff) |
| `--s3-endpoint` | HCP/S3 endpoint URL (env: `HCP_ENDPOINT`) |
| `--address` | Ray cluster address; omit for local mode |
| `--log-level` | `INFO` (default), `DEBUG`, etc. |

## Resumability

The runner lists all input keys and all existing output `.xml` keys, processes only the diff. Re-running a job after a crash resumes from where it stopped.
