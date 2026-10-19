"""Trace every subprocess stack-pr spawns, with wall time.

Put this directory on PYTHONPATH and set SP_TRACE to a file; Python imports
`sitecustomize` automatically at startup, so no stack-pr change is involved.

Each line is {"argv": [...], "elapsed": seconds}. The benchmark uses this for
attribution and for asserting that a workload exercised the path it claims to.
"""

from __future__ import annotations

import json
import os
import subprocess
import threading
import time
from pathlib import Path
from typing import Any

_trace_path = os.environ.get("SP_TRACE")

if _trace_path:
    _handle = Path(_trace_path).open("a", buffering=1)
    _lock = threading.Lock()
    _original_run = subprocess.run

    def _run(args: Any, *pargs: Any, **kwargs: Any) -> Any:
        argv = (
            [str(item) for item in args]
            if isinstance(args, (list, tuple))
            else [str(args)]
        )
        started = time.perf_counter()
        try:
            return _original_run(args, *pargs, **kwargs)
        finally:
            elapsed = round(time.perf_counter() - started, 4)
            line = json.dumps({"argv": argv, "elapsed": elapsed})
            with _lock:
                _handle.write(line + "\n")

    subprocess.run = _run  # type: ignore[assignment]
