"""Console entrypoint — `dummy-runner`, the command the Ray job's entrypoint names."""

import sys

from dummy_runner.job import main

if __name__ == "__main__":
    sys.exit(main())
