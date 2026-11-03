"""Direct worldrpc transport for credential-free growth workers.

The worker runs beside the session containers in the Colima VM, so localhost
and the roster's account-owned port are sufficient. No account config, saved
session key, host CLI, or nested ``colima ssh`` is needed.
"""
from __future__ import annotations

import json
import urllib.error
import urllib.request


class RPCError(RuntimeError):
    """A bounded worldrpc call failed or returned an error."""


def call(port: int, method: str, params: dict | None = None, *,
         timeout: float = 10.0) -> dict:
    payload = json.dumps({
        "jsonrpc": "2.0", "id": 1, "method": method,
        "params": params or {},
    }, separators=(",", ":")).encode("utf-8")
    request = urllib.request.Request(
        f"http://127.0.0.1:{int(port)}/rpc", data=payload,
        headers={"Content-Type": "application/json"}, method="POST")
    try:
        with urllib.request.urlopen(request, timeout=float(timeout)) as response:
            raw = response.read()
    except (OSError, urllib.error.URLError) as exc:
        raise RPCError(f"{method} failed: {exc}") from exc
    try:
        data = json.loads(raw)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise RPCError(f"{method} returned invalid JSON") from exc
    if not isinstance(data, dict):
        raise RPCError(f"{method} returned a non-object response")
    if data.get("error"):
        raise RPCError(f"{method}: {data['error']}")
    result = data.get("result")
    if not isinstance(result, dict):
        raise RPCError(f"{method} returned no object result")
    return result
