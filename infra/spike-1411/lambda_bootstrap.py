"""A dependency-free AWS Lambda runtime bootstrap (spike, issue #1411).

Lambda's container-image runtime contract is an HTTP protocol, not a library:
long-poll ``GET /2018-06-01/runtime/invocation/next``, then ``POST`` either
``/invocation/{id}/response`` or ``/invocation/{id}/error``. The AWS-supplied
client (``awslambdaric``) implements it, but installing it means a ``pip
install`` with a C++ extension **inside the image under measurement** — which
(a) needs cross-architecture emulation to build the x86_64 production image on
an arm64 workstation, and (b) changes the very artifact whose cold start we are
trying to measure. This file implements the same contract in stdlib
``urllib.request``, so the spike's image is the production backend image plus
two ``COPY`` layers and nothing else.

The handler is imported *lazily on first invocation*, not at init. That is
deliberate for measurement: it splits Lambda's reported ``Init Duration``
(container start + interpreter start alone) from the Python import cost of the
backend package, which the probe then times explicitly. Blending them into one
number would hide which half a cold start is actually made of.
"""

from __future__ import annotations

import importlib
import json
import os
import sys
import time
import traceback
import urllib.request

_PROCESS_START = time.time()

_RUNTIME_API = os.environ.get("AWS_LAMBDA_RUNTIME_API", "")
_BASE = f"http://{_RUNTIME_API}/2018-06-01/runtime"
#: ``module.attr`` of the callable to invoke. Overridable so one image can serve
#: both the measurement probe and the real generation entrypoint.
_HANDLER_SPEC = os.environ.get("SPIKE_HANDLER", "spike_probe.handler")


class _Context:
    """The handful of ``context`` attributes a handler may reasonably read."""

    def __init__(self, request_id: str, deadline_ms: str | None) -> None:
        self.aws_request_id = request_id
        self.function_name = os.environ.get("AWS_LAMBDA_FUNCTION_NAME", "")
        self.memory_limit_in_mb = os.environ.get("AWS_LAMBDA_FUNCTION_MEMORY_SIZE", "")
        self._deadline_ms = int(deadline_ms) if deadline_ms else 0
        self.process_start_epoch = _PROCESS_START

    def get_remaining_time_in_millis(self) -> int:
        if not self._deadline_ms:
            return 0
        return max(0, self._deadline_ms - int(time.time() * 1000))


def _post(path: str, payload: dict) -> None:
    request = urllib.request.Request(
        f"{_BASE}{path}",
        data=json.dumps(payload, default=str).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(request) as response:
        response.read()


def _error_payload(exc: BaseException) -> dict:
    return {
        "errorType": type(exc).__name__,
        "errorMessage": str(exc),
        "stackTrace": traceback.format_exc().splitlines()[-25:],
    }


def _load_handler():
    module_name, _, attribute = _HANDLER_SPEC.rpartition(".")
    return getattr(importlib.import_module(module_name), attribute)


def main() -> None:
    if not _RUNTIME_API:
        raise SystemExit("AWS_LAMBDA_RUNTIME_API is unset — this bootstrap only runs inside Lambda")

    handler = None
    while True:
        # No timeout: /invocation/next is a long poll and blocking here is the
        # protocol, not a hang.
        with urllib.request.urlopen(f"{_BASE}/invocation/next") as response:
            request_id = response.headers.get("Lambda-Runtime-Aws-Request-Id", "")
            deadline = response.headers.get("Lambda-Runtime-Deadline-Ms")
            raw = response.read()
        try:
            event = json.loads(raw) if raw else {}
            if handler is None:
                handler = _load_handler()
            result = handler(event, _Context(request_id, deadline))
            _post(f"/invocation/{request_id}/response", result)
        except BaseException as exc:
            print(traceback.format_exc(), file=sys.stderr, flush=True)
            _post(f"/invocation/{request_id}/error", _error_payload(exc))


if __name__ == "__main__":
    main()
