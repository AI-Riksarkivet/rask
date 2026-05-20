"""ra-viewer entry point — runs `uvicorn viewer.app:app`."""

import argparse
import os

import uvicorn


def main() -> None:
    parser = argparse.ArgumentParser(description="ra-viewer FastAPI server")
    parser.add_argument("--input", "-i", help="Input source URI (s3://bucket or filesystem path)")
    parser.add_argument("--output", "-o", help="Output source URI (s3://bucket or filesystem path)")
    parser.add_argument("--host", default="0.0.0.0")
    parser.add_argument("--port", "-p", type=int, default=8888)
    parser.add_argument("--reload", action="store_true", help="Enable auto-reload (dev)")
    args = parser.parse_args()

    if args.input:
        os.environ["RASK_VIEWER_INPUT"] = args.input
    if args.output:
        os.environ["RASK_VIEWER_OUTPUT"] = args.output

    if "RASK_VIEWER_INPUT" not in os.environ or "RASK_VIEWER_OUTPUT" not in os.environ:
        raise SystemExit("Set --input and --output (or RASK_VIEWER_INPUT/RASK_VIEWER_OUTPUT env vars).")

    uvicorn.run("viewer.app:app", host=args.host, port=args.port, reload=args.reload)


if __name__ == "__main__":
    main()
