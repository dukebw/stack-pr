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

    # Tracing Popen covers subprocess.run too, which creates one and waits on it.
    class _TracedPopen(subprocess.Popen):  # type: ignore[type-arg]
        def __init__(self, args: Any, *pargs: Any, **kwargs: Any) -> None:
            self._trace_argv = (
                [str(item) for item in args]
                if isinstance(args, (list, tuple))
                else [str(args)]
            )
            self._trace_started = time.perf_counter()
            self._trace_logged = False
            super().__init__(args, *pargs, **kwargs)

        def wait(self, timeout: Any = None) -> int:
            returncode = super().wait(timeout)
            if not self._trace_logged:
                self._trace_logged = True
                elapsed = round(time.perf_counter() - self._trace_started, 4)
                line = json.dumps({"argv": self._trace_argv, "elapsed": elapsed})
                with _lock:
                    _handle.write(line + "\n")
            return returncode

    subprocess.Popen = _TracedPopen  # type: ignore[misc]
