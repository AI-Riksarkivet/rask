"""Deploy / undeploy Ray Serve apps for ra-runner.

Two apps live behind this script:
  - transcribe : the TrOCR-only deployment (existing; what HTR uses today
                 via `TranscribeViaServe`).
  - htrflow   : the full HTRflow pipeline wrapped as a single deployment
                 (Segmentation -> Segmentation -> TextRecognition ->
                 OrderLines). CPU-only by default — see htrflow_pipeline.yaml
                 to flip individual steps onto cuda.

Run from the repo root with the local Ray cluster up:

    uv run python scripts/deploy_serve.py up                  # default: transcribe
    uv run python scripts/deploy_serve.py up --app htrflow    # full pipeline
    uv run python scripts/deploy_serve.py down --app htrflow
    uv run python scripts/deploy_serve.py status              # all apps

Designed to run as a one-shot from `make serve-up` / `make serve-down`.
"""

import argparse
import os
import sys

import ray
from dotenv import load_dotenv
from ray import serve


# Each entry: (build callable, default route prefix). The `build` callable
# returns a Serve `Application` (the value of `Deployment.bind()`).
def _build_transcribe() -> object:
    from runner.transcribe_service import build_app

    return build_app()


def _build_htrflow() -> object:
    from runner.htrflow_service import htrflow_app

    return htrflow_app


APPS = {
    "transcribe": (_build_transcribe, "/transcribe"),
    "htrflow": (_build_htrflow, "/htrflow"),
}


def _connect() -> None:
    """Attach to the running Ray cluster (does not start a new one)."""
    address = os.environ.get("RAY_ADDRESS", "auto")
    ray.init(address=address, ignore_reinit_error=True, log_to_driver=False)


def cmd_up(app_name: str) -> int:
    _connect()
    build, route = APPS[app_name]
    handle = serve.run(build(), name=app_name, route_prefix=route, blocking=False)
    print(f"Deployed app {app_name!r} at route {route}.")
    print(f"Get a handle with: serve.get_app_handle({app_name!r})")
    print(f"Handle ready: {handle}")
    return 0


def cmd_down(app_name: str) -> int:
    _connect()
    serve.delete(app_name)
    print(f"Deleted app {app_name!r}.")
    return 0


def cmd_status(app_name: str | None = None) -> int:
    _connect()
    status = serve.status()
    apps = getattr(status, "applications", {}) or {}
    targets = [app_name] if app_name else list(APPS)
    rc = 0
    for name in targets:
        if name not in apps:
            print(f"App {name!r} not running.")
            rc = 1
            continue
        app_status = apps[name]
        print(f"App {name!r}: {app_status.status}")
        for dep_name, dep in app_status.deployments.items():
            print(f"  {dep_name}: {dep.status} (replicas: {dep.replica_states})")
    return rc


def main() -> int:
    load_dotenv()
    parser = argparse.ArgumentParser()
    parser.add_argument("action", choices=["up", "down", "status"])
    parser.add_argument(
        "--app",
        choices=list(APPS),
        default="transcribe",
        help="Which Serve app to act on (default: transcribe)",
    )
    args = parser.parse_args()
    if args.action == "up":
        return cmd_up(args.app)
    if args.action == "down":
        return cmd_down(args.app)
    return cmd_status(args.app if "--app" in sys.argv else None)


if __name__ == "__main__":
    sys.exit(main())
