# Argo Workflows with HCP S3

[Argo Workflows](https://argoproj.github.io/workflows/) can use HCP as an S3-compatible artifact store. This enables pipelines to read input data from and write results back to HCP namespaces. The examples below show how to configure Argo to work with the HCP API's S3 credentials endpoint and presigned URLs.

All examples are provided in both **YAML** (native Argo manifests) and **[Hera](https://github.com/argoproj-labs/hera)** (Python SDK for Argo Workflows).

```mermaid
graph LR
    subgraph K8s["Kubernetes"]
        AC["Argo<br/>Controller"]
        SEC[("K8s Secret<br/><small>hcp-s3-credentials</small>")]
        subgraph Pods["Workflow Pods"]
            P1["Pod 1"]
            P2["Pod 2"]
            P3["Pod N"]
        end
        AC -->|schedules| Pods
    end

    subgraph HCP["Hitachi Content Platform"]
        API["HCP Unified API<br/><small>/api/v1</small>"]
        S3[("S3 Buckets &<br/>Namespaces")]
        API --> S3
    end

    SEC -.->|credentials| Pods
    Pods <-->|S3 artifacts or<br/>presigned URLs| API
```

## Configuring HCP S3 credentials for Argo

First, retrieve the S3 credentials from the API and create a Kubernetes Secret that Argo can reference:

=== "curl"

    ```bash
    BASE="http://localhost:8000/api/v1"
    TOKEN="<your-token>"

    # Fetch S3 credentials from the HCP API
    CREDS=$(curl -s "$BASE/credentials" -H "Authorization: Bearer $TOKEN")

    ACCESS_KEY=$(echo "$CREDS" | jq -r .access_key_id)
    SECRET_KEY=$(echo "$CREDS" | jq -r .secret_access_key)
    ENDPOINT=$(echo "$CREDS" | jq -r .endpoint_url)

    # Create Kubernetes Secret for Argo
    kubectl create secret generic hcp-s3-credentials \
      --from-literal=accessKey="$ACCESS_KEY" \
      --from-literal=secretKey="$SECRET_KEY" \
      -n argo
    ```

=== "Python"

    ```python
    import httpx
    import subprocess

    BASE = "http://localhost:8000/api/v1"

    async def create_argo_s3_secret(token: str, namespace: str = "argo"):
        """Fetch HCP S3 credentials and create a Kubernetes Secret for Argo."""
        headers = {"Authorization": f"Bearer {token}"}

        async with httpx.AsyncClient(base_url=BASE, headers=headers) as c:
            resp = await c.get("/credentials")
            resp.raise_for_status()
            creds = resp.json()

        subprocess.run(
            [
                "kubectl", "create", "secret", "generic", "hcp-s3-credentials",
                f"--from-literal=accessKey={creds['access_key_id']}",
                f"--from-literal=secretKey={creds['secret_access_key']}",
                "-n", namespace,
                "--dry-run=client", "-o", "yaml",
            ],
            check=True,
        )
    ```

### Python container images

The Python-based Argo templates use the [`rahcp-client`](../sdk/index.md) SDK for all HCP S3 operations. Build one image:

```dockerfile
FROM python:3.13-slim
RUN pip install --no-cache-dir rahcp-client "rahcp-validate"
```

```bash
docker build -t my-registry/hcp-sdk:3.13 .
docker push my-registry/hcp-sdk:3.13
```

Replace `my-registry/` with your actual container registry path.

#### What the SDK image provides

| Package | Purpose |
|---------|---------|
| **rahcp-client** | Async HTTP client with presigned URL transfers, multipart uploads, retries, and token refresh |
| **rahcp-validate** | Image validation (TIFF, JPEG) with magic-byte checks and Pillow verification |

#### Quick reference

| SDK method | Description |
|------------|-------------|
| `HCPClient.from_env()` | Create client from `HCP_*` env vars (or mounted Secret) |
| `client.s3.upload(bucket, key, data)` | Upload via presigned URL (auto multipart for large files) |
| `client.s3.download(bucket, key, dest)` | Download via presigned URL to local path |
| `client.s3.download_bytes(bucket, key)` | Download to bytes |
| `client.s3.head(bucket, key)` | Get object metadata (HEAD) |
| `client.s3.list_objects(bucket, prefix)` | List objects under a prefix |
| `client.s3.delete_bulk(bucket, keys)` | Bulk-delete object keys |
| `client.s3.commit_staging(bucket, staging, dest)` | Copy staging to final prefix, then delete staging |
| `client.s3.cleanup_staging(bucket, staging)` | Delete all objects under staging prefix |
| `validate_tiff(path)` | Check TIFF magic bytes + Pillow verify |
| `validate_jpg(path)` | Check JPEG magic bytes + full Pillow decode |

See the [Python SDK documentation](../sdk/index.md) for full API details.

### Installing Hera

[Hera](https://github.com/argoproj-labs/hera) lets you define Argo Workflows entirely in Python instead of YAML. Install it with:

```bash
uv add hera
```

---

## ETL pipeline with HCP S3 artifacts

A workflow that reads a dataset from HCP, processes it, and writes results back:

```mermaid
graph LR
    subgraph HCP["HCP S3"]
        IN[("manifests/<br/>latest.json")]
        OUT[("results/<br/>output.json")]
    end

    subgraph Argo["Argo DAG"]
        E["extract-data"]
        T["transform-data"]
        L["load-results"]
        E -->|artifact| T -->|artifact| L
    end

    IN -.->|S3 input<br/>artifact| E
    L -.->|S3 output<br/>artifact| OUT
```

=== "YAML"

    ```yaml
    apiVersion: argoproj.io/v1alpha1
    kind: Workflow
    metadata:
      generateName: hcp-etl-
    spec:
      entrypoint: etl-pipeline
      activeDeadlineSeconds: 1800        # workflow-level timeout: 30 min
      artifactGC:
        strategy: OnWorkflowDeletion
      templates:
        - name: etl-pipeline
          dag:
            tasks:
              - name: extract
                template: extract-data
              - name: transform
                template: transform-data
                dependencies: [extract]
              - name: load
                template: load-results
                dependencies: [transform]

        - name: extract-data
          retryStrategy:
            limit: 3
            backoff:
              duration: "5s"
              factor: 2
              maxDuration: "1m"
          container:
            image: python:3.13-slim
            command: [python, -c]
            args:
              - |
                from pathlib import Path
                import json
                # Read the input artifact (mounted by Argo from HCP S3)
                data = json.loads(Path("/tmp/input/manifest.json").read_text())
                print(f"Loaded {len(data['files'])} files from manifest")
                # Write output for next step
                Path("/tmp/output/extracted.json").write_text(json.dumps(data))
          inputs:
            artifacts:
              - name: input-manifest
                path: /tmp/input/manifest.json
                s3:
                  endpoint: hcp-s3.example.com
                  bucket: datasets
                  key: manifests/latest.json
                  accessKeySecret:
                    name: hcp-s3-credentials
                    key: accessKey
                  secretKeySecret:
                    name: hcp-s3-credentials
                    key: secretKey
                  insecure: false
          outputs:
            artifacts:
              - name: extracted
                path: /tmp/output/extracted.json

        - name: transform-data
          retryStrategy:
            limit: 3
            backoff:
              duration: "5s"
              factor: 2
              maxDuration: "1m"
          container:
            image: python:3.13-slim
            command: [python, -c]
            args:
              - |
                from pathlib import Path
                import json
                data = json.loads(Path("/tmp/input/extracted.json").read_text())
                results = {"processed": len(data.get("files", [])), "status": "ok"}
                Path("/tmp/output/results.json").write_text(json.dumps(results))
          inputs:
            artifacts:
              - name: extracted
                path: /tmp/input/extracted.json
          outputs:
            artifacts:
              - name: results
                path: /tmp/output/results.json

        - name: load-results
          retryStrategy:
            limit: 3
            backoff:
              duration: "5s"
              factor: 2
              maxDuration: "1m"
          container:
            image: python:3.13-slim
            command: [python, -c]
            args:
              - |
                from pathlib import Path
                data = Path("/tmp/input/results.json").read_text()
                print(f"Results uploaded to HCP: {data}")
          inputs:
            artifacts:
              - name: results
                path: /tmp/input/results.json
          outputs:
            artifacts:
              - name: final-results
                path: /tmp/input/results.json
                s3:
                  endpoint: hcp-s3.example.com
                  bucket: results
                  key: "etl/{{workflow.name}}/results.json"
                  accessKeySecret:
                    name: hcp-s3-credentials
                    key: accessKey
                  secretKeySecret:
                    name: hcp-s3-credentials
                    key: secretKey
    ```

=== "Hera"

    ```python
    from hera.workflows import (
        DAG,
        Artifact,
        S3Artifact,
        Workflow,
        models as m,
        script,
    )

    HCP_S3 = m.S3Artifact(
        endpoint="hcp-s3.example.com",
        bucket="datasets",
        access_key_secret=m.SecretKeySelector(name="hcp-s3-credentials", key="accessKey"),
        secret_key_secret=m.SecretKeySelector(name="hcp-s3-credentials", key="secretKey"),
        insecure=False,
    )

    RETRY = m.RetryStrategy(
        limit="3",
        backoff=m.Backoff(duration="5s", factor=2, max_duration="1m"),
    )


    @script(image="python:3.13-slim", retry_strategy=RETRY)
    def extract_data(manifest: Artifact) -> Artifact:
        """Read input from HCP S3, write extracted data."""
        from pathlib import Path
        import json

        data = json.loads(Path("/tmp/input/manifest.json").read_text())
        print(f"Loaded {len(data['files'])} files from manifest")
        Path("/tmp/output/extracted.json").write_text(json.dumps(data))


    @script(image="python:3.13-slim", retry_strategy=RETRY)
    def transform_data(extracted: Artifact) -> Artifact:
        """Process the extracted data."""
        from pathlib import Path
        import json

        data = json.loads(Path("/tmp/input/extracted.json").read_text())
        results = {"processed": len(data.get("files", [])), "status": "ok"}
        Path("/tmp/output/results.json").write_text(json.dumps(results))


    @script(image="python:3.13-slim", retry_strategy=RETRY)
    def load_results(results: Artifact) -> Artifact:
        """Upload results back to HCP S3."""
        from pathlib import Path

        data = Path("/tmp/input/results.json").read_text()
        print(f"Results uploaded to HCP: {data}")


    with Workflow(
        generate_name="hcp-etl-",
        entrypoint="etl-pipeline",
        active_deadline_seconds=1800,
        artifact_gc=m.ArtifactGC(strategy="OnWorkflowDeletion"),
    ) as w:
        with DAG(name="etl-pipeline"):
            ext = extract_data(
                name="extract",
                arguments=[
                    S3Artifact(
                        name="manifest",
                        path="/tmp/input/manifest.json",
                        **HCP_S3.dict() | {"key": "manifests/latest.json"},
                    ),
                ],
            )
            trn = transform_data(
                name="transform",
                arguments=[ext.get_artifact("extracted").with_name("extracted")],
            )
            ld = load_results(
                name="load",
                arguments=[trn.get_artifact("results").with_name("results")],
            )
            ext >> trn >> ld

    w.create()  # submit to the Argo server
    ```

=== "rahcp SDK (Hera)"

    Using the `rahcp-client` SDK instead of Argo S3 artifacts — no S3 credentials Secret needed:

    ```python
    from hera.workflows import DAG, Workflow, models as m, script

    IMAGE = "my-registry/hcp-sdk:3.13"
    RETRY = m.RetryStrategy(limit="3", backoff=m.Backoff(duration="5s", factor=2))

    # Credentials injected as env vars from K8s Secret
    HCP_ENV = [
        m.EnvVar(name="HCP_ENDPOINT", value="http://hcp-api.default.svc:8000/api/v1"),
        m.EnvVar(name="HCP_USERNAME", value_from=m.EnvVarSource(
            secret_key_ref=m.SecretKeySelector(name="hcp-credentials", key="username"))),
        m.EnvVar(name="HCP_PASSWORD", value_from=m.EnvVarSource(
            secret_key_ref=m.SecretKeySelector(name="hcp-credentials", key="password"))),
        m.EnvVar(name="HCP_TENANT", value_from=m.EnvVarSource(
            secret_key_ref=m.SecretKeySelector(name="hcp-credentials", key="tenant"))),
    ]


    @script(image=IMAGE, retry_strategy=RETRY, env=HCP_ENV)
    def extract_data(bucket: str, key: str):
        """Download manifest from HCP via presigned URL."""
        import asyncio, json
        from pathlib import Path
        from rahcp_client import HCPClient

        async def main():
            async with HCPClient.from_env() as client:
                data = await client.s3.download_bytes(bucket, key)
                manifest = json.loads(data)
                print(f"Loaded {len(manifest['files'])} files")
                Path("/tmp/extracted.json").write_text(json.dumps(manifest))

        asyncio.run(main())


    @script(image=IMAGE, retry_strategy=RETRY)
    def transform_data():
        """Process the extracted data."""
        import json
        from pathlib import Path

        data = json.loads(Path("/tmp/extracted.json").read_text())
        results = {"processed": len(data.get("files", [])), "status": "ok"}
        Path("/tmp/results.json").write_text(json.dumps(results))


    @script(image=IMAGE, retry_strategy=RETRY, env=HCP_ENV)
    def load_results(bucket: str):
        """Upload results back to HCP via presigned URL."""
        import asyncio
        from pathlib import Path
        from rahcp_client import HCPClient

        async def main():
            async with HCPClient.from_env() as client:
                await client.s3.upload(bucket, "results/output.json", Path("/tmp/results.json"))
                print("Results uploaded")

        asyncio.run(main())


    with Workflow(
        generate_name="hcp-etl-sdk-",
        entrypoint="etl-pipeline",
        active_deadline_seconds=1800,
    ) as w:
        with DAG(name="etl-pipeline"):
            ext = extract_data(name="extract", arguments={
                "bucket": "datasets", "key": "manifests/latest.json"})
            trn = transform_data(name="transform")
            ld = load_results(name="load", arguments={"bucket": "results"})
            ext >> trn >> ld

    w.create()
    ```

=== "rahcp SDK (YAML)"

    Standalone runnable YAML — uses the SDK in script blocks with credentials from a K8s Secret. No Hera dependency.

    ```yaml
    --8<-- "examples/argo-etl-pipeline.yaml:workflow"
    ```

---

## Presigned URL pipeline

!!! tip "The SDK handles presigned URLs automatically"
    When using the `rahcp-client` SDK (as in the batch and cross-tenant examples below), presigned URLs are generated and used internally — you never need to manage them manually. This section shows the **manual approach** for cases where you need explicit control over URL generation.

For cases where you cannot mount S3 credentials into every pod, use the HCP API to generate presigned URLs and pass them as parameters:

```mermaid
sequenceDiagram
    participant Argo as Argo Steps
    participant API as HCP API
    participant S3 as HCP S3

    rect rgb(240,248,255)
    Note over Argo: Step 1 — presign
    Argo->>API: POST /presign (get_object)
    API-->>Argo: download URL
    Argo->>API: POST /presign (put_object)
    API-->>Argo: upload URL
    end

    rect rgb(245,255,245)
    Note over Argo: Step 2 — process
    Argo->>S3: GET presigned download URL
    S3-->>Argo: input data
    Note over Argo: process data...
    Argo->>S3: PUT presigned upload URL
    S3-->>Argo: 200 OK
    end
```

=== "YAML"

    ```yaml
    apiVersion: argoproj.io/v1alpha1
    kind: Workflow
    metadata:
      generateName: hcp-presign-
    spec:
      entrypoint: presigned-pipeline
      activeDeadlineSeconds: 900         # workflow-level timeout: 15 min
      arguments:
        parameters:
          - name: hcp-api-base
            value: "http://hcp-api.default.svc:8000/api/v1"
          - name: hcp-token
            value: "<your-token>"
          - name: bucket
            value: "datasets"
      templates:
        - name: presigned-pipeline
          steps:
            - - name: generate-urls
                template: presign
            - - name: process
                template: process-with-urls
                arguments:
                  parameters:
                    - name: download-url
                      value: "{{steps.generate-urls.outputs.parameters.download-url}}"
                    - name: upload-url
                      value: "{{steps.generate-urls.outputs.parameters.upload-url}}"

        - name: presign
          retryStrategy:
            limit: 3
            backoff:
              duration: "5s"
              factor: 2
          script:
            image: curlimages/curl:latest
            command: [sh]
            source: |
              BASE="{{workflow.parameters.hcp-api-base}}"
              TOKEN="{{workflow.parameters.hcp-token}}"
              BUCKET="{{workflow.parameters.bucket}}"

              # Get download URL for input
              DL=$(curl -s -X POST "$BASE/presign" \
                -H "Authorization: Bearer $TOKEN" \
                -H "Content-Type: application/json" \
                -d "{\"bucket\":\"$BUCKET\",\"key\":\"input/data.csv\",\"method\":\"get_object\",\"expires_in\":3600}")
              echo "$DL" | grep -o '"url":"[^"]*"' | cut -d'"' -f4 > /tmp/download-url

              # Get upload URL for output
              UL=$(curl -s -X POST "$BASE/presign" \
                -H "Authorization: Bearer $TOKEN" \
                -H "Content-Type: application/json" \
                -d "{\"bucket\":\"$BUCKET\",\"key\":\"output/result.csv\",\"method\":\"put_object\",\"expires_in\":3600}")
              echo "$UL" | grep -o '"url":"[^"]*"' | cut -d'"' -f4 > /tmp/upload-url
          outputs:
            parameters:
              - name: download-url
                valueFrom:
                  path: /tmp/download-url
              - name: upload-url
                valueFrom:
                  path: /tmp/upload-url

        - name: process-with-urls
          retryStrategy:
            limit: 2
            backoff:
              duration: "10s"
              factor: 2
          inputs:
            parameters:
              - name: download-url
              - name: upload-url
          script:
            image: my-registry/python-httpx:3.13
            command: [python]
            source: |
              import httpx
              from pathlib import Path

              # Download input via presigned URL (no credentials needed)
              resp = httpx.get("{{inputs.parameters.download-url}}")
              resp.raise_for_status()
              Path("/tmp/data.csv").write_bytes(resp.content)

              # Process...
              Path("/tmp/result.csv").write_text("processed,data\n")

              # Upload result via presigned URL
              data = Path("/tmp/result.csv").read_bytes()
              httpx.put("{{inputs.parameters.upload-url}}", content=data).raise_for_status()
              print("Done: input downloaded and result uploaded via presigned URLs")
    ```

=== "Hera"

    ```python
    from hera.workflows import (
        Parameter,
        Steps,
        Workflow,
        models as m,
        script,
    )

    HCP_BASE = "http://hcp-api.default.svc:8000/api/v1"

    RETRY = m.RetryStrategy(
        limit="3",
        backoff=m.Backoff(duration="5s", factor=2),
    )


    @script(image="my-registry/python-httpx:3.13", retry_strategy=RETRY)
    def generate_presigned_urls(
        hcp_api_base: str,
        hcp_token: str,
        bucket: str,
    ):
        """Generate download and upload presigned URLs from the HCP API."""
        import httpx
        from pathlib import Path

        headers = {"Authorization": f"Bearer {hcp_token}"}

        def presign(key: str, method: str) -> str:
            resp = httpx.post(
                f"{hcp_api_base}/presign",
                json={"bucket": bucket, "key": key, "method": method, "expires_in": 3600},
                headers=headers,
            )
            resp.raise_for_status()
            return resp.json()["url"]

        dl = presign("input/data.csv", "get_object")
        ul = presign("output/result.csv", "put_object")

        Path("/tmp/download-url").write_text(dl)
        Path("/tmp/upload-url").write_text(ul)


    @script(
        image="my-registry/python-httpx:3.13",
        retry_strategy=m.RetryStrategy(limit="2", backoff=m.Backoff(duration="10s", factor=2)),
    )
    def process_with_urls(download_url: str, upload_url: str):
        """Download input, process, and upload result via presigned URLs."""
        import httpx
        from pathlib import Path

        resp = httpx.get(download_url)
        resp.raise_for_status()
        Path("/tmp/data.csv").write_bytes(resp.content)

        Path("/tmp/result.csv").write_text("processed,data\n")

        data = Path("/tmp/result.csv").read_bytes()
        httpx.put(upload_url, content=data).raise_for_status()
        print("Done: input downloaded and result uploaded via presigned URLs")


    with Workflow(
        generate_name="hcp-presign-",
        entrypoint="presigned-pipeline",
        active_deadline_seconds=900,
        arguments=[
            Parameter(name="hcp-api-base", value=HCP_BASE),
            Parameter(name="hcp-token", value="<your-token>"),
            Parameter(name="bucket", value="datasets"),
        ],
    ) as w:
        with Steps(name="presigned-pipeline"):
            urls = generate_presigned_urls(
                name="generate-urls",
                arguments={
                    "hcp_api_base": "{{workflow.parameters.hcp-api-base}}",
                    "hcp_token": "{{workflow.parameters.hcp-token}}",
                    "bucket": "{{workflow.parameters.bucket}}",
                },
            )
            process_with_urls(
                name="process",
                arguments={
                    "download_url": urls.get_parameter("download-url"),
                    "upload_url": urls.get_parameter("upload-url"),
                },
            )

    w.create()
    ```

---

## Running Hera workflows

```bash
# Submit directly from a script
uv run --with hera python etl_workflow.py

# Or export to YAML and submit with Argo CLI
uv run --with hera python -c "
from etl_workflow import w
print(w.to_yaml())
" | argo submit -

# Useful during development: validate without submitting
uv run --with hera python -c "
from etl_workflow import w
print(w.to_yaml())
" | argo lint -
```

---

## Batch processing -- fan-out over HCP objects

A common pattern: list objects from an HCP bucket, process each one in parallel (fan-out), then aggregate results (fan-in).

```mermaid
graph TD
    D["discover<br/><small>list objects</small>"]
    D --> F

    subgraph F["fan-out (parallel)"]
        direction LR
        P1["process<br/>obj-1"]
        P2["process<br/>obj-2"]
        P3["process<br/>obj-N"]
    end

    F --> A["summarize<br/><small>aggregate results</small>"]

    IN[("incoming/<br/>*.csv")] -.-> D
    P1 & P2 & P3 -.->|presigned PUT| OUT[("processed/<br/>*.result.json")]
    A -.-> SUM[("summaries/<br/>batch.json")]
```

=== "YAML"

    ```yaml
    apiVersion: argoproj.io/v1alpha1
    kind: Workflow
    metadata:
      generateName: hcp-batch-
    spec:
      entrypoint: batch-pipeline
      arguments:
        parameters:
          - name: hcp-api-base
            value: "http://hcp-api.default.svc:8000/api/v1"
          - name: hcp-token
            value: "<your-token>"
          - name: bucket
            value: "datasets"
          - name: prefix
            value: "incoming/"
      templates:
        # ── Orchestrator DAG ─────────────────────────────────────────
        - name: batch-pipeline
          dag:
            tasks:
              - name: discover
                template: list-objects
              - name: process
                template: fan-out
                dependencies: [discover]
                arguments:
                  parameters:
                    - name: object-keys
                      value: "{{tasks.discover.outputs.parameters.keys}}"
              - name: summarize
                template: aggregate
                dependencies: [process]

        # ── Step 1: List objects from HCP via the API ────────────────
        - name: list-objects
          retryStrategy:
            limit: 3
            backoff:
              duration: "5s"
              factor: 2
          script:
            image: curlimages/curl:latest
            command: [sh]
            source: |
              BASE="{{workflow.parameters.hcp-api-base}}"
              TOKEN="{{workflow.parameters.hcp-token}}"
              BUCKET="{{workflow.parameters.bucket}}"
              PREFIX="{{workflow.parameters.prefix}}"

              RESP=$(curl -s -f "$BASE/buckets/$BUCKET/objects?prefix=$PREFIX&max_keys=100" \
                -H "Authorization: Bearer $TOKEN")

              # Extract object keys as a JSON array
              echo "$RESP" | jq '[.objects[].key]' > /tmp/keys.json
              echo "Found $(echo "$RESP" | jq '.objects | length') objects"
              cat /tmp/keys.json
          outputs:
            parameters:
              - name: keys
                valueFrom:
                  path: /tmp/keys.json

        # ── Step 2: Fan out — one pod per object ─────────────────────
        - name: fan-out
          inputs:
            parameters:
              - name: object-keys
          steps:
            - - name: process-object
                template: process-single
                arguments:
                  parameters:
                    - name: key
                      value: "{{item}}"
                withParam: "{{inputs.parameters.object-keys}}"

        - name: process-single
          retryStrategy:
            limit: 2
            backoff:
              duration: "10s"
              factor: 2
          inputs:
            parameters:
              - name: key
          script:
            image: my-registry/hcp-sdk:3.13
            command: [python]
            env:
              - name: HCP_ENDPOINT
                value: "{{workflow.parameters.hcp-api-base}}"
              - name: HCP_USERNAME
                valueFrom:
                  secretKeyRef:
                    name: hcp-credentials
                    key: username
              - name: HCP_PASSWORD
                valueFrom:
                  secretKeyRef:
                    name: hcp-credentials
                    key: password
              - name: HCP_TENANT
                valueFrom:
                  secretKeyRef:
                    name: hcp-credentials
                    key: tenant
            source: |
              import asyncio, json
              from pathlib import Path
              from rahcp_client import HCPClient

              async def main():
                  async with HCPClient.from_env() as client:
                      BUCKET = "{{workflow.parameters.bucket}}"
                      KEY = "{{inputs.parameters.key}}"

                      # Download via presigned URL
                      size = await client.s3.download(BUCKET, KEY, Path("/tmp/data"))

                      # Process (placeholder — replace with your logic)
                      result = {"key": KEY, "size": size, "status": "processed"}
                      print(json.dumps(result))

                      # Upload result via presigned URL
                      result_key = KEY.replace("incoming/", "processed/") + ".result.json"
                      await client.s3.upload(BUCKET, result_key, json.dumps(result).encode())
                      print(f"Result uploaded to {result_key}")

              asyncio.run(main())

        # ── Step 3: Fan in — aggregate results ───────────────────────
        - name: aggregate
          retryStrategy:
            limit: 2
          script:
            image: curlimages/curl:latest
            command: [sh]
            source: |
              BASE="{{workflow.parameters.hcp-api-base}}"
              TOKEN="{{workflow.parameters.hcp-token}}"
              BUCKET="{{workflow.parameters.bucket}}"

              # List all processed results
              RESULTS=$(curl -s -f "$BASE/buckets/$BUCKET/objects?prefix=processed/&max_keys=1000" \
                -H "Authorization: Bearer $TOKEN")

              COUNT=$(echo "$RESULTS" | jq '.objects | length')
              SUMMARY="{\"workflow\":\"{{workflow.name}}\",\"objects_processed\":$COUNT,\"status\":\"complete\"}"
              echo "$SUMMARY"

              # Upload summary via presigned URL
              PRESIGN=$(curl -s -X POST "$BASE/presign" \
                -H "Authorization: Bearer $TOKEN" \
                -H "Content-Type: application/json" \
                -d "{\"bucket\":\"$BUCKET\",\"key\":\"summaries/{{workflow.name}}.json\",\"method\":\"put_object\",\"expires_in\":600}")
              UPLOAD_URL=$(echo "$PRESIGN" | jq -r '.url')
              curl -s -X PUT "$UPLOAD_URL" -d "$SUMMARY"
              echo "Summary uploaded"
    ```

=== "Hera"

    ```python
    from hera.workflows import (
        DAG,
        Parameter,
        Steps,
        Workflow,
        models as m,
        script,
    )

    HCP_BASE = "http://hcp-api.default.svc:8000/api/v1"

    RETRY = m.RetryStrategy(
        limit="3",
        backoff=m.Backoff(duration="5s", factor=2),
    )


    @script(image="my-registry/hcp-sdk:3.13", retry_strategy=RETRY)
    def list_objects(bucket: str, prefix: str):
        """List objects from HCP and output their keys as a JSON array."""
        import asyncio, json
        from pathlib import Path
        from rahcp_client import HCPClient

        async def main():
            async with HCPClient.from_env() as client:
                result = await client.s3.list_objects(bucket, prefix=prefix, max_keys=100)
                keys = [obj["key"] for obj in result["objects"]]
                print(f"Found {len(keys)} objects")
                Path("/tmp/keys.json").write_text(json.dumps(keys))

        asyncio.run(main())


    @script(
        image="my-registry/hcp-sdk:3.13",
        retry_strategy=m.RetryStrategy(limit="2", backoff=m.Backoff(duration="10s", factor=2)),
    )
    def process_single(bucket: str, key: str):
        """Download an object via presigned URL, process it, upload the result."""
        import asyncio, json
        from pathlib import Path
        from rahcp_client import HCPClient

        async def main():
            async with HCPClient.from_env() as client:
                # Download via presigned URL
                size = await client.s3.download(bucket, key, Path("/tmp/data"))

                # Process (placeholder — replace with your logic)
                result = {"key": key, "size": size, "status": "processed"}
                print(json.dumps(result))

                # Upload result via presigned URL
                result_key = key.replace("incoming/", "processed/") + ".result.json"
                await client.s3.upload(bucket, result_key, json.dumps(result).encode())
                print(f"Result uploaded to {result_key}")

        asyncio.run(main())


    @script(image="my-registry/hcp-sdk:3.13", retry_strategy=RETRY)
    def aggregate(bucket: str):
        """List processed results and upload a summary."""
        import asyncio, json
        from rahcp_client import HCPClient

        async def main():
            async with HCPClient.from_env() as client:
                result = await client.s3.list_objects(bucket, prefix="processed/")
                summary = json.dumps({"objects_processed": len(result["objects"]), "status": "complete"})
                print(summary)
                await client.s3.upload(bucket, "summaries/batch-result.json", summary.encode())

        asyncio.run(main())


    WF_PARAMS = {
        "hcp_api_base": "{{workflow.parameters.hcp-api-base}}",
        "hcp_token": "{{workflow.parameters.hcp-token}}",
        "bucket": "{{workflow.parameters.bucket}}",
    }

    with Workflow(
        generate_name="hcp-batch-",
        entrypoint="batch-pipeline",
        arguments=[
            Parameter(name="hcp-api-base", value=HCP_BASE),
            Parameter(name="hcp-token", value="<your-token>"),
            Parameter(name="bucket", value="datasets"),
            Parameter(name="prefix", value="incoming/"),
        ],
    ) as w:
        with DAG(name="batch-pipeline"):
            # Step 1: Discover objects
            disc = list_objects(
                name="discover",
                arguments={**WF_PARAMS, "prefix": "{{workflow.parameters.prefix}}"},
            )
            # Step 2: Fan out — process each object in parallel
            with Steps(name="fan-out") as fan:
                process_single(
                    name="process-object",
                    arguments={
                        **WF_PARAMS,
                        "key": "{{item}}",
                    },
                    with_param=disc.get_parameter("keys"),
                )
            # Step 3: Aggregate results
            agg = aggregate(name="summarize", arguments=WF_PARAMS)

            disc >> fan >> agg

    w.create()
    ```

=== "rahcp SDK (YAML)"

    Standalone runnable YAML — uses the SDK in script blocks with credentials from a K8s Secret. No Hera dependency.

    ```yaml
    --8<-- "examples/argo-batch-fanout.yaml:workflow"
    ```

!!! tip "Which approach to use?"
    - **S3 artifacts** (YAML or Hera DAG): Best when Argo has direct network access to HCP S3. Argo handles download/upload automatically. Requires the S3 credentials Secret.
    - **Presigned URLs** (YAML or Hera Steps): Best when pods cannot reach HCP directly or you want to avoid distributing S3 credentials. The HCP API generates short-lived URLs that anyone can use.
    - **Batch fan-out**: Use `withParam` (YAML) or `with_param` (Hera) to process N objects in parallel. Argo handles scheduling and concurrency limits.
    - **YAML vs Hera**: Use YAML for simple workflows or when non-Python teams maintain them. Use Hera when you want type safety, IDE autocompletion, and Python-native DAG composition (`>>` operator).

---

## Cross-tenant data transformation -- TIFF to JPG

A common archival workflow: read TIFF images from an **ingest** tenant, convert them to JPG, and write the results to a **publish** tenant. Each tenant has its own credentials, and the workflow never exposes one tenant's token to the other.

```mermaid
graph LR
    subgraph SRC["Source Tenant"]
        S3A[("ingest/<br/>scans/*.tiff")]
    end

    subgraph Argo["Argo Workflow"]
        direction TB
        LIST["list-objects<br/><small>list ingest/*.tiff</small>"]
        LIST --> FAN

        subgraph FAN["fan-out (parallel)"]
            direction LR
            C1["convert<br/>scan-001"]
            C2["convert<br/>scan-002"]
            C3["convert<br/>scan-N"]
        end

        FAN --> VER["verify<br/><small>count JPGs</small>"]
    end

    subgraph DST["Destination Tenant"]
        S3B[("publish/<br/>images/*.jpg")]
    end

    S3A -.->|presigned GET<br/><small>source token</small>| FAN
    FAN -.->|presigned PUT<br/><small>dest token</small>| S3B

    style SRC fill:#e8f4fd,stroke:#0d6efd
    style DST fill:#d4edda,stroke:#28a745
```

### Security model

Each tenant has its own HCP credentials stored in a separate Kubernetes Secret. The `rahcp-client` SDK authenticates on startup using these credentials and transfers data via **presigned URLs** -- pods never access HCP S3 directly.

```bash
# Create secrets for each tenant (run once)
kubectl create secret generic hcp-source-creds \
  --from-literal=username="<source-username>" \
  --from-literal=password="<source-password>" \
  --from-literal=tenant="<source-tenant>" \
  -n argo

kubectl create secret generic hcp-dest-creds \
  --from-literal=username="<dest-username>" \
  --from-literal=password="<dest-password>" \
  --from-literal=tenant="<dest-tenant>" \
  -n argo
```

### Workflow definition

=== "YAML"

    Each pod gets credentials via environment variables from Kubernetes Secrets. The `rahcp-client` SDK handles authentication and presigned URL transfers automatically.

    ```yaml
    apiVersion: argoproj.io/v1alpha1
    kind: Workflow
    metadata:
      generateName: hcp-tiff-to-jpg-
    spec:
      entrypoint: convert-pipeline
      activeDeadlineSeconds: 7200         # 2 hours max
      onExit: cleanup
      arguments:
        parameters:
          - name: hcp-api-base
            value: "http://hcp-api.default.svc:8000/api/v1"
          - name: source-bucket
            value: "scans"
          - name: source-prefix
            value: "ingest/"
          - name: dest-bucket
            value: "images"
      templates:
        # ── Orchestrator ──────────────────────────────────────────────
        - name: convert-pipeline
          dag:
            tasks:
              - name: discover
                template: list-tiffs
              - name: convert
                template: fan-out
                dependencies: [discover]
                arguments:
                  parameters:
                    - name: object-keys
                      value: "{{tasks.discover.outputs.parameters.keys}}"
              - name: verify
                template: verify-and-commit
                dependencies: [convert]

        # ── List TIFF files in the source tenant ──────────────────────
        - name: list-tiffs
          retryStrategy:
            limit: 3
            backoff:
              duration: "5s"
              factor: 2
          script:
            image: my-registry/hcp-sdk:3.13
            command: [python]
            env: &source-env
              - name: HCP_ENDPOINT
                value: "{{workflow.parameters.hcp-api-base}}"
              - name: HCP_USERNAME
                valueFrom:
                  secretKeyRef: { name: hcp-source-creds, key: username }
              - name: HCP_PASSWORD
                valueFrom:
                  secretKeyRef: { name: hcp-source-creds, key: password }
              - name: HCP_TENANT
                valueFrom:
                  secretKeyRef: { name: hcp-source-creds, key: tenant }
            source: |
              import asyncio, json
              from pathlib import Path
              from rahcp_client import HCPClient

              async def main():
                  async with HCPClient.from_env() as client:
                      result = await client.s3.list_objects(
                          "{{workflow.parameters.source-bucket}}",
                          prefix="{{workflow.parameters.source-prefix}}",
                          max_keys=500,
                      )
                      keys = [o["key"] for o in result["objects"]
                              if o["key"].lower().endswith((".tiff", ".tif"))]
                      print(f"Found {len(keys)} TIFF files")
                      Path("/tmp/keys.json").write_text(json.dumps(keys))

              asyncio.run(main())
          outputs:
            parameters:
              - name: keys
                valueFrom:
                  path: /tmp/keys.json

        # ── Fan out — one conversion pod per TIFF ─────────────────────
        - name: fan-out
          inputs:
            parameters:
              - name: object-keys
          steps:
            - - name: convert-tiff
                template: convert-single
                arguments:
                  parameters:
                    - name: key
                      value: "{{item}}"
                withParam: "{{inputs.parameters.object-keys}}"

        - name: convert-single
          retryStrategy:
            limit: 2
            retryPolicy: Always
            backoff:
              duration: "15s"
              factor: 2
              maxDuration: "2m"
          activeDeadlineSeconds: 600        # 10 min per image
          inputs:
            parameters:
              - name: key
          script:
            image: my-registry/hcp-sdk:3.13
            command: [python]
            env:
              - name: HCP_ENDPOINT
                value: "{{workflow.parameters.hcp-api-base}}"
              - name: HCP_USERNAME
                valueFrom:
                  secretKeyRef: { name: hcp-source-creds, key: username }
              - name: HCP_PASSWORD
                valueFrom:
                  secretKeyRef: { name: hcp-source-creds, key: password }
              - name: HCP_TENANT
                valueFrom:
                  secretKeyRef: { name: hcp-source-creds, key: tenant }
              - name: DST_USERNAME
                valueFrom:
                  secretKeyRef: { name: hcp-dest-creds, key: username }
              - name: DST_PASSWORD
                valueFrom:
                  secretKeyRef: { name: hcp-dest-creds, key: password }
              - name: DST_TENANT
                valueFrom:
                  secretKeyRef: { name: hcp-dest-creds, key: tenant }
            resources:
              requests:
                memory: "512Mi"
                cpu: "500m"
              limits:
                memory: "1Gi"
            source: |
              import asyncio, os
              from pathlib import Path
              from PIL import Image
              from rahcp_client import HCPClient
              from rahcp_validate.images import validate_tiff, validate_jpg

              async def main():
                  endpoint = os.environ["HCP_ENDPOINT"]
                  SRC_BUCKET = "{{workflow.parameters.source-bucket}}"
                  DST_BUCKET = "{{workflow.parameters.dest-bucket}}"
                  KEY = "{{inputs.parameters.key}}"
                  WF = "{{workflow.name}}"

                  src = HCPClient(endpoint=endpoint,
                      username=os.environ["HCP_USERNAME"],
                      password=os.environ["HCP_PASSWORD"],
                      tenant=os.environ["HCP_TENANT"])
                  dst = HCPClient(endpoint=endpoint,
                      username=os.environ["DST_USERNAME"],
                      password=os.environ["DST_PASSWORD"],
                      tenant=os.environ["DST_TENANT"])

                  async with src, dst:
                      # 1. Download and validate TIFF
                      tiff_path = Path("/tmp/input.tiff")
                      size = await src.s3.download(SRC_BUCKET, KEY, tiff_path)
                      validate_tiff(tiff_path)
                      print(f"Downloaded and validated {KEY} ({size} bytes)")

                      # 2. Convert TIFF → JPG and validate output
                      jpg_path = Path("/tmp/output.jpg")
                      img = Image.open(tiff_path)
                      img.convert("RGB").save(jpg_path, "JPEG", quality=85)
                      validate_jpg(jpg_path)
                      print(f"Converted to JPG ({img.size[0]}x{img.size[1]})")

                      # 3. Upload to staging and verify
                      filename = KEY.split("/")[-1].rsplit(".", 1)[0] + ".jpg"
                      staging_key = f"staging/{WF}/{filename}"
                      await dst.s3.upload(DST_BUCKET, staging_key, jpg_path)
                      meta = await dst.s3.head(DST_BUCKET, staging_key)
                      print(f"Uploaded and verified {staging_key}")

              asyncio.run(main())

        # ── Commit staging → published ────────────────────────────────
        - name: verify-and-commit
          retryStrategy:
            limit: 2
          script:
            image: my-registry/hcp-sdk:3.13
            command: [python]
            env: &dest-env
              - name: HCP_ENDPOINT
                value: "{{workflow.parameters.hcp-api-base}}"
              - name: HCP_USERNAME
                valueFrom:
                  secretKeyRef: { name: hcp-dest-creds, key: username }
              - name: HCP_PASSWORD
                valueFrom:
                  secretKeyRef: { name: hcp-dest-creds, key: password }
              - name: HCP_TENANT
                valueFrom:
                  secretKeyRef: { name: hcp-dest-creds, key: tenant }
            source: |
              import asyncio
              from rahcp_client import HCPClient

              async def main():
                  async with HCPClient.from_env() as client:
                      count = await client.s3.commit_staging(
                          "{{workflow.parameters.dest-bucket}}",
                          "staging/{{workflow.name}}/",
                          "published/",
                      )
                      print(f"Committed {count} JPGs to published/")

              asyncio.run(main())

        # ── Exit handler — clean up staging on failure ────────────────
        - name: cleanup
          script:
            image: my-registry/hcp-sdk:3.13
            command: [python]
            env: *dest-env
            source: |
              import asyncio

              STATUS = "{{workflow.status}}"
              if STATUS == "Succeeded":
                  print("Workflow succeeded — no cleanup needed")
                  exit(0)

              from rahcp_client import HCPClient

              async def main():
                  async with HCPClient.from_env() as client:
                      print(f"Workflow {STATUS} — cleaning up staging/...")
                      deleted = await client.s3.cleanup_staging(
                          "{{workflow.parameters.dest-bucket}}",
                          "staging/{{workflow.name}}/",
                      )
                      print(f"Deleted {deleted} staged objects")

              asyncio.run(main())
    ```

=== "Hera"

    Each `@script` function uses the `rahcp-client` SDK. Credentials are injected via environment variables from Kubernetes Secrets.

    ```python
    from hera.workflows import (
        DAG,
        Parameter,
        Steps,
        Workflow,
        models as m,
        script,
    )

    HCP_BASE = "http://hcp-api.default.svc:8000/api/v1"
    IMAGE = "my-registry/hcp-sdk:3.13"

    RETRY = m.RetryStrategy(limit="3", backoff=m.Backoff(duration="5s", factor=2))
    RETRY_CONVERT = m.RetryStrategy(
        limit="2", retry_policy="Always",
        backoff=m.Backoff(duration="15s", factor=2, max_duration="2m"),
    )

    # Environment variables from K8s Secrets
    def _secret_env(secret_name: str, prefix: str = "HCP") -> list[m.EnvVar]:
        return [
            m.EnvVar(name=f"{prefix}_ENDPOINT", value=HCP_BASE),
            m.EnvVar(name=f"{prefix}_USERNAME", value_from=m.EnvVarSource(
                secret_key_ref=m.SecretKeySelector(name=secret_name, key="username"))),
            m.EnvVar(name=f"{prefix}_PASSWORD", value_from=m.EnvVarSource(
                secret_key_ref=m.SecretKeySelector(name=secret_name, key="password"))),
            m.EnvVar(name=f"{prefix}_TENANT", value_from=m.EnvVarSource(
                secret_key_ref=m.SecretKeySelector(name=secret_name, key="tenant"))),
        ]

    SRC_ENV = _secret_env("hcp-source-creds")
    DST_ENV = _secret_env("hcp-dest-creds")
    BOTH_ENV = SRC_ENV + _secret_env("hcp-dest-creds", prefix="DST")


    @script(image=IMAGE, retry_strategy=RETRY, env=SRC_ENV)
    def list_tiffs(source_bucket: str, source_prefix: str):
        """List TIFF files in the source tenant."""
        import asyncio, json
        from pathlib import Path
        from rahcp_client import HCPClient

        async def main():
            async with HCPClient.from_env() as client:
                result = await client.s3.list_objects(source_bucket, prefix=source_prefix, max_keys=500)
                keys = [o["key"] for o in result["objects"]
                        if o["key"].lower().endswith((".tiff", ".tif"))]
                print(f"Found {len(keys)} TIFF files")
                Path("/tmp/keys.json").write_text(json.dumps(keys))

        asyncio.run(main())


    @script(
        image=IMAGE,
        retry_strategy=RETRY_CONVERT,
        active_deadline_seconds=600,
        env=BOTH_ENV,
        resources=m.ResourceRequirements(
            requests={"memory": "512Mi", "cpu": "500m"},
            limits={"memory": "1Gi"},
        ),
    )
    def convert_single(source_bucket: str, dest_bucket: str, key: str, workflow_name: str):
        """Download TIFF, validate, convert to JPG, validate, upload, verify."""
        import asyncio, os
        from pathlib import Path
        from PIL import Image
        from rahcp_client import HCPClient
        from rahcp_validate.images import validate_tiff, validate_jpg

        async def main():
            endpoint = os.environ["HCP_ENDPOINT"]
            src = HCPClient(endpoint=endpoint,
                username=os.environ["HCP_USERNAME"],
                password=os.environ["HCP_PASSWORD"],
                tenant=os.environ["HCP_TENANT"])
            dst = HCPClient(endpoint=endpoint,
                username=os.environ["DST_USERNAME"],
                password=os.environ["DST_PASSWORD"],
                tenant=os.environ["DST_TENANT"])

            async with src, dst:
                # 1. Download and validate TIFF
                tiff_path = Path("/tmp/input.tiff")
                size = await src.s3.download(source_bucket, key, tiff_path)
                validate_tiff(tiff_path)
                print(f"Downloaded and validated {key} ({size} bytes)")

                # 2. Convert TIFF → JPG and validate output
                jpg_path = Path("/tmp/output.jpg")
                img = Image.open(tiff_path)
                img.convert("RGB").save(jpg_path, "JPEG", quality=85)
                validate_jpg(jpg_path)
                print(f"Converted to JPG ({img.size[0]}x{img.size[1]})")

                # 3. Upload to staging and verify
                filename = key.split("/")[-1].rsplit(".", 1)[0] + ".jpg"
                staging_key = f"staging/{workflow_name}/{filename}"
                await dst.s3.upload(dest_bucket, staging_key, jpg_path)
                await dst.s3.head(dest_bucket, staging_key)
                print(f"Uploaded and verified {staging_key}")

        asyncio.run(main())


    @script(image=IMAGE, retry_strategy=m.RetryStrategy(limit="2"), env=DST_ENV)
    def verify_and_commit(dest_bucket: str, workflow_name: str):
        """Commit staged JPGs to published/ prefix."""
        import asyncio
        from rahcp_client import HCPClient

        async def main():
            async with HCPClient.from_env() as client:
                count = await client.s3.commit_staging(
                    dest_bucket,
                    f"staging/{workflow_name}/",
                    "published/",
                )
                print(f"Committed {count} JPGs to published/")

        asyncio.run(main())


    @script(image=IMAGE, env=DST_ENV)
    def cleanup(dest_bucket: str, workflow_name: str):
        """Exit handler: delete staging prefix on failure."""
        import asyncio, os
        from rahcp_client import HCPClient

        status = os.environ.get("ARGO_WORKFLOW_STATUS", "Unknown")
        if status == "Succeeded":
            print("Workflow succeeded — no cleanup needed")
            return

        async def main():
            async with HCPClient.from_env() as client:
                print(f"Workflow {status} — cleaning up staging/{workflow_name}/...")
                deleted = await client.s3.cleanup_staging(dest_bucket, f"staging/{workflow_name}/")
                print(f"Deleted {deleted} staged objects")

        asyncio.run(main())


    with Workflow(
        generate_name="hcp-tiff-to-jpg-",
        entrypoint="convert-pipeline",
        active_deadline_seconds=7200,
        on_exit="cleanup",
        arguments=[
            Parameter(name="source-bucket", value="scans"),
            Parameter(name="source-prefix", value="ingest/"),
            Parameter(name="dest-bucket", value="images"),
        ],
    ) as w:
        with DAG(name="convert-pipeline"):
            disc = list_tiffs(
                name="discover",
                arguments={
                    "source_bucket": "{{workflow.parameters.source-bucket}}",
                    "source_prefix": "{{workflow.parameters.source-prefix}}",
                },
            )
            with Steps(name="fan-out") as fan:
                convert_single(
                    name="convert-tiff",
                    arguments={
                        "source_bucket": "{{workflow.parameters.source-bucket}}",
                        "dest_bucket": "{{workflow.parameters.dest-bucket}}",
                        "key": "{{item}}",
                        "workflow_name": "{{workflow.name}}",
                    },
                    with_param=disc.get_parameter("keys"),
                )
            ver = verify_and_commit(
                name="verify",
                arguments={
                    "dest_bucket": "{{workflow.parameters.dest-bucket}}",
                    "workflow_name": "{{workflow.name}}",
                },
            )
            disc >> fan >> ver

        cleanup(
            name="cleanup",
            arguments={
                "dest_bucket": "{{workflow.parameters.dest-bucket}}",
                "workflow_name": "{{workflow.name}}",
            },
        )

    w.create()
    ```

=== "rahcp SDK (YAML)"

    Standalone runnable YAML — uses the SDK in script blocks with dual-tenant credentials from K8s Secrets. No Hera dependency.

    ```yaml
    --8<-- "examples/argo-tiff-to-jpg.yaml:workflow"
    ```

### Verification pipeline

Each image goes through a 3-stage verification before it reaches the `published/` prefix:

```mermaid
graph LR
    DL["Download TIFF"] --> VT["Validate TIFF<br/><small>magic bytes + Pillow verify()</small>"]
    VT --> CONV["Convert<br/>TIFF → JPG"]
    CONV --> VJ["Validate JPG<br/><small>magic bytes + Pillow load()</small>"]
    VJ --> UP["Upload to<br/>staging/"]
    UP --> VU["Verify upload<br/><small>HEAD Content-Length</small>"]
    VU --> OK["Staged ✓"]

    style VT fill:#e8f4fd,stroke:#0d6efd
    style VJ fill:#e8f4fd,stroke:#0d6efd
    style VU fill:#e8f4fd,stroke:#0d6efd
    style OK fill:#d4edda,stroke:#28a745
```

| Step | What it catches |
|------|----------------|
| `validate_tiff` (magic bytes + `Image.verify()`) | Corrupted downloads, truncated files, non-TIFF files |
| `validate_jpg` (magic bytes + `Image.load()`) | Conversion failures, truncated output, Pillow encoding errors |
| `verify_upload` (HEAD Content-Length) | Incomplete uploads, network drops during PUT, S3 eventual-consistency delays |

The key security and reliability properties:

1. **Tenant isolation** -- each tenant's JWT is stored in a separate K8s Secret, mounted read-only into only the pods that need it.
2. **Presigned URLs** -- conversion pods never see raw S3 credentials. URLs are scoped to a single object and expire in 10 minutes.
3. **3-stage verification** -- every image is validated after download, after conversion, and after upload. Corrupt or truncated files are caught immediately and the pod retries.
4. **Staged-commit** -- JPGs are written to `staging/{workflow-name}/` in the destination tenant, then committed to `published/` only if all conversions succeed.
5. **Exit handler cleanup** -- if any conversion pod fails, the exit handler deletes all staged objects from the destination tenant.
6. **DRY helpers** -- all presigning, transfer, and verification logic is handled by the `rahcp-client` SDK, shared across every step.

---

## Error handling for batch workflows

The batch fan-out above processes objects independently, but what happens when some pods fail? Without cleanup, partial results pollute the output prefix. This section adds an **exit handler** that cleans up on failure, plus a **staged-commit** pattern so partial results are never visible to downstream consumers.

```mermaid
graph TD
    D["discover<br/><small>list objects</small>"]
    D --> F

    subgraph F["fan-out (parallel)"]
        direction LR
        P1["process<br/>obj-1"]
        P2["process<br/>obj-2"]
        P3["process<br/>obj-N"]
    end

    F -->|all succeed| C["commit<br/><small>copy staging→processed</small>"]
    C --> DONE["Succeeded"]

    F -->|any fails| FAIL["Failed"]
    FAIL --> CL["cleanup (onExit)<br/><small>delete staging/</small>"]

    subgraph S3["HCP S3 Prefixes"]
        direction LR
        STG[("staging/<br/>workflow-id/")]
        FINAL[("processed/")]
    end

    P1 & P2 & P3 -.->|write| STG
    C -.->|copy| FINAL
    C -.->|delete| STG
    CL -.->|delete| STG

    style DONE fill:#d4edda,stroke:#28a745
    style FAIL fill:#f8d7da,stroke:#dc3545
    style CL fill:#fff3cd,stroke:#ffc107
```

=== "YAML"

    ```yaml
    apiVersion: argoproj.io/v1alpha1
    kind: Workflow
    metadata:
      generateName: hcp-batch-safe-
    spec:
      entrypoint: batch-pipeline
      activeDeadlineSeconds: 3600       # hard limit: 1 hour
      onExit: cleanup                   # always runs, even on failure
      arguments:
        parameters:
          - name: hcp-api-base
            value: "http://hcp-api.default.svc:8000/api/v1"
          - name: hcp-token
            value: "<your-token>"
          - name: bucket
            value: "datasets"
          - name: prefix
            value: "incoming/"
      templates:
        # ── Orchestrator ─────────────────────────────────────────────
        - name: batch-pipeline
          dag:
            tasks:
              - name: discover
                template: list-objects
              - name: process
                template: fan-out
                dependencies: [discover]
                arguments:
                  parameters:
                    - name: object-keys
                      value: "{{tasks.discover.outputs.parameters.keys}}"
              - name: commit
                template: commit-results
                dependencies: [process]

        # ── List objects ─────────────────────────────────────────────
        - name: list-objects
          retryStrategy:
            limit: 3
            backoff:
              duration: "5s"
              factor: 2
          script:
            image: curlimages/curl:latest
            command: [sh]
            source: |
              BASE="{{workflow.parameters.hcp-api-base}}"
              TOKEN="{{workflow.parameters.hcp-token}}"
              BUCKET="{{workflow.parameters.bucket}}"
              PREFIX="{{workflow.parameters.prefix}}"

              RESP=$(curl -s -f "$BASE/buckets/$BUCKET/objects?prefix=$PREFIX&max_keys=100" \
                -H "Authorization: Bearer $TOKEN")
              echo "$RESP" | jq '[.objects[].key]' > /tmp/keys.json
              echo "Found $(echo "$RESP" | jq '.objects | length') objects"
          outputs:
            parameters:
              - name: keys
                valueFrom:
                  path: /tmp/keys.json

        # ── Fan out — write to staging prefix ────────────────────────
        - name: fan-out
          inputs:
            parameters:
              - name: object-keys
          steps:
            - - name: process-object
                template: process-single
                arguments:
                  parameters:
                    - name: key
                      value: "{{item}}"
                withParam: "{{inputs.parameters.object-keys}}"

        - name: process-single
          retryStrategy:
            limit: 2
            retryPolicy: Always        # retry on OOM kills and node failures too
            backoff:
              duration: "10s"
              factor: 2
              maxDuration: "2m"
          activeDeadlineSeconds: 300    # per-pod timeout: 5 min
          inputs:
            parameters:
              - name: key
          script:
            image: my-registry/hcp-sdk:3.13
            command: [python]
            env: &hcp-env
              - name: HCP_ENDPOINT
                value: "{{workflow.parameters.hcp-api-base}}"
              - name: HCP_USERNAME
                valueFrom:
                  secretKeyRef: { name: hcp-credentials, key: username }
              - name: HCP_PASSWORD
                valueFrom:
                  secretKeyRef: { name: hcp-credentials, key: password }
              - name: HCP_TENANT
                valueFrom:
                  secretKeyRef: { name: hcp-credentials, key: tenant }
            source: |
              import asyncio, json
              from pathlib import Path
              from rahcp_client import HCPClient

              async def main():
                  async with HCPClient.from_env() as client:
                      BUCKET = "{{workflow.parameters.bucket}}"
                      KEY = "{{inputs.parameters.key}}"
                      WF = "{{workflow.name}}"

                      # Download via presigned URL
                      size = await client.s3.download(BUCKET, KEY, Path("/tmp/data"))

                      # Process (placeholder — replace with your logic)
                      result = json.dumps({"key": KEY, "size": size, "status": "processed"})

                      # Write to STAGING prefix — not visible to consumers yet
                      staging_key = f"staging/{WF}/{KEY.split('/')[-1]}.result.json"
                      await client.s3.upload(BUCKET, staging_key, result.encode())
                      print(f"Staged: {staging_key}")

              asyncio.run(main())

        # ── Commit — copy staging → final prefix ─────────────────────
        - name: commit-results
          retryStrategy:
            limit: 2
          script:
            image: my-registry/hcp-sdk:3.13
            command: [python]
            env: *hcp-env
            source: |
              import asyncio
              from rahcp_client import HCPClient

              async def main():
                  async with HCPClient.from_env() as client:
                      count = await client.s3.commit_staging(
                          "{{workflow.parameters.bucket}}",
                          "staging/{{workflow.name}}/",
                          "processed/",
                      )
                      print(f"Committed {count} results, staging cleaned up")

              asyncio.run(main())

        # ── Exit handler — clean up staging on failure ────────────────
        - name: cleanup
          script:
            image: curlimages/curl:latest
            command: [sh]
            source: |
              BASE="{{workflow.parameters.hcp-api-base}}"
              TOKEN="{{workflow.parameters.hcp-token}}"
              BUCKET="{{workflow.parameters.bucket}}"
              WF="{{workflow.name}}"
              STATUS="{{workflow.status}}"

              if [ "$STATUS" != "Succeeded" ]; then
                echo "Workflow $STATUS — deleting staging prefix staging/$WF/..."
                # List staged objects
                KEYS=$(curl -s -f "$BASE/buckets/$BUCKET/objects?prefix=staging/$WF/&max_keys=1000" \
                  -H "Authorization: Bearer $TOKEN" | jq '[.objects[].key]')

                if [ "$(echo "$KEYS" | jq 'length')" -gt 0 ]; then
                  curl -s -X POST "$BASE/buckets/$BUCKET/objects/delete" \
                    -H "Authorization: Bearer $TOKEN" \
                    -H "Content-Type: application/json" \
                    -d "{\"keys\": $KEYS}" || true
                  echo "Staging cleaned up"
                else
                  echo "No staged objects to clean up"
                fi
              else
                echo "Workflow succeeded — no cleanup needed"
              fi
    ```

=== "Hera"

    ```python
    from hera.workflows import (
        DAG,
        Parameter,
        Steps,
        Workflow,
        models as m,
        script,
    )

    HCP_BASE = "http://hcp-api.default.svc:8000/api/v1"
    IMAGE = "my-registry/hcp-sdk:3.13"

    # Inject credentials from K8s Secret as env vars
    HCP_ENV = [
        m.EnvVar(name="HCP_ENDPOINT", value=HCP_BASE),
        m.EnvVar(name="HCP_USERNAME", value_from=m.EnvVarSource(
            secret_key_ref=m.SecretKeySelector(name="hcp-credentials", key="username"))),
        m.EnvVar(name="HCP_PASSWORD", value_from=m.EnvVarSource(
            secret_key_ref=m.SecretKeySelector(name="hcp-credentials", key="password"))),
        m.EnvVar(name="HCP_TENANT", value_from=m.EnvVarSource(
            secret_key_ref=m.SecretKeySelector(name="hcp-credentials", key="tenant"))),
    ]

    RETRY = m.RetryStrategy(limit="3", backoff=m.Backoff(duration="5s", factor=2))
    RETRY_POD = m.RetryStrategy(
        limit="2", retry_policy="Always",
        backoff=m.Backoff(duration="10s", factor=2, max_duration="2m"),
    )


    @script(image=IMAGE, retry_strategy=RETRY, env=HCP_ENV)
    def list_objects(bucket: str, prefix: str):
        """List objects and output keys as JSON array."""
        import asyncio, json
        from pathlib import Path
        from rahcp_client import HCPClient

        async def main():
            async with HCPClient.from_env() as client:
                result = await client.s3.list_objects(bucket, prefix=prefix, max_keys=100)
                keys = [obj["key"] for obj in result["objects"]]
                print(f"Found {len(keys)} objects")
                Path("/tmp/keys.json").write_text(json.dumps(keys))

        asyncio.run(main())


    @script(image=IMAGE, retry_strategy=RETRY_POD, active_deadline_seconds=300, env=HCP_ENV)
    def process_single(bucket: str, key: str, workflow_name: str):
        """Download, process, and write result to staging prefix."""
        import asyncio, json
        from pathlib import Path
        from rahcp_client import HCPClient

        async def main():
            async with HCPClient.from_env() as client:
                size = await client.s3.download(bucket, key, Path("/tmp/data"))
                result = json.dumps({"key": key, "size": size, "status": "processed"})
                staging_key = f"staging/{workflow_name}/{key.split('/')[-1]}.result.json"
                await client.s3.upload(bucket, staging_key, result.encode())
                print(f"Staged: {staging_key}")

        asyncio.run(main())


    @script(image=IMAGE, retry_strategy=m.RetryStrategy(limit="2"), env=HCP_ENV)
    def commit_results(bucket: str, workflow_name: str):
        """Copy staging → processed, then delete staging."""
        import asyncio
        from rahcp_client import HCPClient

        async def main():
            async with HCPClient.from_env() as client:
                count = await client.s3.commit_staging(
                    bucket, f"staging/{workflow_name}/", "processed/",
                )
                print(f"Committed {count} results, staging cleaned up")

        asyncio.run(main())


    @script(image=IMAGE, env=HCP_ENV)
    def cleanup(bucket: str, workflow_name: str):
        """Exit handler: delete staging prefix on failure."""
        import asyncio, os
        from rahcp_client import HCPClient

        status = os.environ.get("ARGO_WORKFLOW_STATUS", "Unknown")
        if status == "Succeeded":
            print("Workflow succeeded — no cleanup needed")
            return

        async def main():
            async with HCPClient.from_env() as client:
                print(f"Workflow {status} — cleaning up staging/{workflow_name}/...")
                deleted = await client.s3.cleanup_staging(bucket, f"staging/{workflow_name}/")
                print(f"Deleted {deleted} staged objects")

        asyncio.run(main())


    with Workflow(
        generate_name="hcp-batch-safe-",
        entrypoint="batch-pipeline",
        active_deadline_seconds=3600,
        on_exit="cleanup",
        arguments=[
            Parameter(name="hcp-api-base", value=HCP_BASE),
            Parameter(name="bucket", value="datasets"),
            Parameter(name="prefix", value="incoming/"),
        ],
    ) as w:
        with DAG(name="batch-pipeline"):
            disc = list_objects(
                name="discover",
                arguments={
                    "bucket": "{{workflow.parameters.bucket}}",
                    "prefix": "{{workflow.parameters.prefix}}",
                },
            )
            with Steps(name="fan-out") as fan:
                process_single(
                    name="process-object",
                    arguments={
                        "bucket": "{{workflow.parameters.bucket}}",
                        "key": "{{item}}",
                        "workflow_name": "{{workflow.name}}",
                    },
                    with_param=disc.get_parameter("keys"),
                )
            com = commit_results(
                name="commit",
                arguments={
                    "bucket": "{{workflow.parameters.bucket}}",
                    "workflow_name": "{{workflow.name}}",
                },
            )
            disc >> fan >> com

        cleanup(
            name="cleanup",
            arguments={
                "bucket": "{{workflow.parameters.bucket}}",
                "workflow_name": "{{workflow.name}}",
            },
        )

    w.create()
    ```

The key patterns in this workflow:

1. **Staged writes** -- fan-out pods write to `staging/{{workflow.name}}/` instead of the final `processed/` prefix. Downstream consumers never see partial results.
2. **Commit step** -- only runs if ALL fan-out pods succeed. Uses `client.s3.commit_staging()` to copy staging to final prefix, then deletes staging.
3. **Exit handler (`onExit: cleanup`)** -- guaranteed to run on failure. Uses `client.s3.cleanup_staging()` to delete the staging prefix.
4. **Per-pod retries** -- `retryPolicy: Always` retries on OOM kills and node evictions (not just script errors). `activeDeadlineSeconds: 300` prevents hung pods.
5. **Workflow timeout** -- `activeDeadlineSeconds: 3600` ensures the entire workflow fails rather than running forever.

---

## Runnable examples

The `examples/` directory contains standalone, runnable versions of the workflows on this page. These files are the **single source of truth** — the "rahcp SDK (YAML)" tabs above include directly from them via snippet markers, so docs and examples can never drift.

| File | Pattern |
|------|---------|
| [`examples/argo-etl-pipeline.yaml`](https://github.com/riksarkivet/ra-hcp/blob/main/examples/argo-etl-pipeline.yaml) | ETL DAG with SDK script blocks |
| [`examples/argo-batch-fanout.yaml`](https://github.com/riksarkivet/ra-hcp/blob/main/examples/argo-batch-fanout.yaml) | Fan-out over HCP objects with SDK |
| [`examples/argo-tiff-to-jpg.yaml`](https://github.com/riksarkivet/ra-hcp/blob/main/examples/argo-tiff-to-jpg.yaml) | Cross-tenant TIFF→JPG with staging + exit handler |

```bash
# Submit directly
argo submit examples/argo-etl-pipeline.yaml

# Lint without submitting
argo lint examples/argo-batch-fanout.yaml
```

---

## Related pages

- [Python SDK](../sdk/index.md) -- `rahcp-client` async client with automatic retries, presigned URLs, and multipart uploads.
- [API Workflows](workflows.md) -- curl and Python examples for authentication, S3 operations, tenant/namespace management, and more.
- [Error Handling](error-handling.md) -- Retries, exit handlers, ACID patterns, and Argo-native retry/timeout configuration.
