"""Deproi example worker: a long-running job runner, used as a protected service.

`python worker.py --busy` is its busy check: exit 1 while a job runs (the file
/tmp/job exists), exit 0 when idle. Deproi does not recreate it while busy.
"""

import pathlib
import sys
import time

JOB = pathlib.Path("/tmp/job")  # noqa: S108 — a marker inside the container

if __name__ == "__main__":
    if sys.argv[1:] == ["--busy"]:
        sys.exit(1 if JOB.exists() else 0)
    while True:
        time.sleep(5)
