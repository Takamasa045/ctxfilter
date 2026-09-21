"""Local deterministic read routing. No network or inference dependencies."""

from __future__ import annotations

import json
import os

from ctxfilter.core import (
    Limits, ToolError, _stat_fields, dumps_result, error_envelope,
    open_regular, resolve_regular_file, run_action,
)

DEFAULT_SMALL_BYTES = 4096
DEFAULT_OUTPUT_BYTES = 4096
MAX_SMALL_BYTES = 16384


def _encoded(obj: dict) -> str:
    return json.dumps(obj, ensure_ascii=False, separators=(",", ":"))


def _bounded(obj: dict, limits: Limits, route: str, reason: str) -> dict:
    obj = {**obj, "route": route, "reason": reason}
    result = json.loads(dumps_result(obj, limits.max_output_bytes))
    # Even a budget error keeps the routing decision when space permits.
    if "route" not in result:
        result = {"ok": False, "tool": "read", "route": route, "reason": reason,
                  "error": {"code": "output_budget"}, "scan_complete": False}
    return result


def read_file(path: str, *, query: str | None = None,
              small_bytes: int = DEFAULT_SMALL_BYTES, limits: Limits | None = None,
              from_byte: int = 0, identity: dict | None = None) -> dict:
    """Return complete UTF-8 text only when stat size AND serialized size fit.

    Otherwise preserve the existing preview/search identity and cursor contract.
    max_chars/max_lines govern excerpts, not the compact full-text branch.
    """
    limits = (limits or Limits(max_output_bytes=DEFAULT_OUTPUT_BYTES)).clamp()
    route, reason = "error", "invalid_input"
    try:
        if isinstance(small_bytes, bool) or not isinstance(small_bytes, int):
            raise ToolError("invalid_input", "small_bytes must be an integer")
        small_bytes = max(0, min(MAX_SMALL_BYTES, small_bytes))
        if query is not None:
            return _bounded(run_action("search", path=path, query=query, limits=limits,
                                       from_byte=from_byte, identity=identity),
                            limits, "search", "literal_query")
        if from_byte != 0 or identity is not None:
            raise ToolError("invalid_input", "search continuation requires query; use fetch_excerpt for reads")
        resolved = resolve_regular_file(path)
        handle, before = open_regular(resolved)
        try:
            # No sample/hash/probe/body read is needed to select the large route.
            size = before.st_size
            route, reason = "preview", "size_threshold"
            if size <= small_bytes and size <= limits.max_scan_bytes:
                raw = handle.read(size + 1)
                if len(raw) != size:
                    raise ToolError("changed_during_read", "file changed during read")
                if b"\x00" in raw:
                    reason = "binary"
                else:
                    try:
                        content = raw.decode("utf-8")
                    except UnicodeDecodeError:
                        raise ToolError("invalid_utf8", "file is not valid UTF-8")
                    full = {"ok": True, "tool": "read", "route": "full",
                            "reason": "small_and_fits", "size_bytes": size,
                            "text": content}
                    if len(_encoded(full).encode("utf-8")) <= limits.max_output_bytes:
                        result = full
                        route, reason = "full", "small_and_fits"
                    else:
                        reason = "output_budget"
            elif size <= small_bytes:
                reason = "scan_budget"
            if route != "full":
                result = _bounded(run_action("preview", path=str(resolved), limits=limits),
                                  limits, route, reason)
            # Verify both the open descriptor and pathname (including replacements).
            if (_stat_fields(os.fstat(handle.fileno())) != _stat_fields(before)
                    or _stat_fields(os.stat(resolved)) != _stat_fields(before)):
                raise ToolError("changed_during_read", "file changed during read")
            return result
        finally:
            handle.close()
    except ToolError as exc:
        return _bounded(error_envelope("read", exc.code, exc.message), limits, route, reason)
    except PermissionError:
        return _bounded(error_envelope("read", "permission_denied", "permission denied"), limits, route, reason)
    except (OSError, ValueError, TypeError, UnicodeError):
        return _bounded(error_envelope("read", "invalid_input", "input is invalid"), limits, route, reason)
