"""Stdio MCP server: JSON-RPC 2.0, one JSON object per newline."""

from __future__ import annotations

import json
import sys
from typing import Any, BinaryIO

from ctxfilter import __version__
from ctxfilter.auto import read_file, DEFAULT_SMALL_BYTES, DEFAULT_OUTPUT_BYTES
from ctxfilter.core import (
    DEFAULT_FETCH_CHARS,
    DEFAULT_MAX_MATCHES,
    DEFAULT_PREVIEW_CHARS,
    DEFAULT_PREVIEW_LINES,
    HARD_MAX_LINES,
    Limits,
    dumps_result,
    parse_identity,
    result_ok,
    run_action,
)

PROTOCOL_VERSION = "2024-11-05"
SERVER_NAME = "ctxfilter"
MAX_MESSAGE_BYTES = 32_768


def _read_message(stdin: BinaryIO) -> dict[str, Any] | None:
    buf = bytearray()
    while True:
        byte = stdin.read(1)
        if byte == b"":
            if not buf:
                return None
            return {"jsonrpc": "2.0", "id": None, "error": {"code": -32700, "message": "Parse error"}}
        if byte == b"\n":
            break
        buf += byte
        if len(buf) > MAX_MESSAGE_BYTES:
            discarded = 0
            while True:
                extra = stdin.read(1)
                if extra in (b"", b"\n"):
                    break
                discarded += 1
                if discarded > MAX_MESSAGE_BYTES:
                    return {"jsonrpc": "2.0", "id": None, "error": {"code": -32600, "message": "Message too large"}}
            return {"jsonrpc": "2.0", "id": None, "error": {"code": -32600, "message": "Message too large"}}
    if buf.endswith(b"\r"):
        del buf[-1]
    if not buf:
        return _read_message(stdin)
    try:
        parsed = json.loads(buf.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return {"jsonrpc": "2.0", "id": None, "error": {"code": -32700, "message": "Parse error"}}
    if not isinstance(parsed, dict):
        return {"jsonrpc": "2.0", "id": None, "error": {"code": -32600, "message": "Invalid Request"}}
    return parsed


def _write_message(stdout: BinaryIO, message: dict[str, Any]) -> None:
    body = json.dumps(message, ensure_ascii=False, separators=(",", ":"))
    stdout.write(body.encode("utf-8") + b"\n")
    stdout.flush()


def _tool_schemas() -> list[dict[str, Any]]:
    path_prop = {"type": "string", "description": "Local file path. Output is bounded."}
    readonly = {
        "readOnlyHint": True,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": False,
    }
    return [
        {
            "name": "preview_file",
            "description": "Read-only short prefix of one file, with a cursor for later fetch.",
            "inputSchema": {
                "type": "object",
                "properties": {
                    "path": path_prop,
                    "max_chars": {"type": "integer", "minimum": 1, "maximum": 8000},
                    "max_lines": {"type": "integer", "minimum": 1, "maximum": 200},
                },
                "required": ["path"],
            },
            "annotations": readonly,
        },
        {
            "name": "search_file",
            "description": "Literal search in one file. Continue with from_byte and identity; no total unless scan finished.",
            "inputSchema": {
                "type": "object",
                "properties": {
                    "path": path_prop,
                    "query": {"type": "string", "maxLength": 200},
                    "max_matches": {"type": "integer", "minimum": 1, "maximum": 50},
                    "from_byte": {"type": "integer", "minimum": 0},
                    "identity": {
                        "type": "object",
                        "description": "size, mtime_ns, inode, and dev are decimal strings.",
                    },
                },
                "required": ["path", "query"],
            },
            "annotations": readonly,
        },
        {
            "name": "fetch_excerpt",
            "description": "Read more of the same file from a previous identity and offset.",
            "inputSchema": {
                "type": "object",
                "properties": {
                    "path": path_prop,
                    "identity": {
                        "type": "object",
                        "description": "size, mtime_ns, inode, and dev are decimal strings.",
                    },
                    "byte_offset": {"type": "integer", "minimum": 0},
                    "char_offset": {"type": "integer", "minimum": 0},
                    "line": {"type": "integer", "minimum": 1},
                    "max_chars": {"type": "integer", "minimum": 1, "maximum": 8000},
                },
                "required": ["path", "identity"],
            },
            "annotations": readonly,
        },
        {
            "name": "read_file",
            "description": "Default local file read: compact full UTF-8 if small and within output budget, otherwise preview with identity/cursor. query selects literal search, never Jev. Continue incomplete search with next_search_byte and identity; continue previews with fetch_excerpt. Budget covers content JSON, not MCP envelope.",
            "inputSchema": {
                "type": "object",
                "properties": {
                    "path": path_prop,
                    "query": {"type": "string", "minLength": 1, "maxLength": 200},
                    "small_bytes": {"type": "integer", "minimum": 0, "maximum": 16384, "default": 4096},
                    "max_output_bytes": {"type": "integer", "minimum": 256, "maximum": 16384, "default": 4096},
                    "max_chars": {"type": "integer", "minimum": 1, "maximum": 8000},
                    "max_lines": {"type": "integer", "minimum": 1, "maximum": 200},
                    "max_matches": {"type": "integer", "minimum": 1, "maximum": 50},
                    "from_byte": {"type": "integer", "minimum": 0},
                    "identity": {"type": "object", "description": "Unmodified identity from the previous search."},
                },
                "required": ["path"],
                "additionalProperties": False,
            },
            "annotations": readonly,
        },
    ]


def _int_arg(arguments: dict[str, Any], name: str) -> int | None:
    if name not in arguments or arguments[name] is None:
        return None
    try:
        return int(arguments[name])
    except (TypeError, ValueError):
        raise ValueError(name)


def _handle_tool(name: str, arguments: Any) -> dict[str, Any]:
    if not isinstance(arguments, dict):
        arguments = {}
    path = arguments.get("path")
    if name == "read_file":
        allowed = {"path", "query", "small_bytes", "max_output_bytes", "max_chars", "max_lines", "max_matches", "from_byte", "identity"}
        if set(arguments) - allowed:
            raise ValueError("unknown argument")
        for key in allowed - {"path", "query", "identity"}:
            if key in arguments and (isinstance(arguments[key], bool) or not isinstance(arguments[key], int)):
                raise ValueError(key)
        limits = Limits(
            max_output_bytes=arguments.get("max_output_bytes", DEFAULT_OUTPUT_BYTES),
            max_chars=arguments.get("max_chars", DEFAULT_PREVIEW_CHARS),
            max_lines=arguments.get("max_lines", DEFAULT_PREVIEW_LINES),
            max_matches=arguments.get("max_matches", DEFAULT_MAX_MATCHES),
        )
        return read_file(path, query=arguments.get("query"),
                         small_bytes=arguments.get("small_bytes", DEFAULT_SMALL_BYTES),
                         limits=limits, from_byte=arguments.get("from_byte", 0),
                         identity=arguments.get("identity"))
    if name == "preview_file":
        limits = Limits(
            max_chars=_int_arg(arguments, "max_chars") or DEFAULT_PREVIEW_CHARS,
            max_lines=_int_arg(arguments, "max_lines") or DEFAULT_PREVIEW_LINES,
        )
        return run_action("preview", path=path, limits=limits)
    if name == "search_file":
        limits = Limits(max_matches=_int_arg(arguments, "max_matches") or DEFAULT_MAX_MATCHES)
        identity_raw = arguments.get("identity")
        identity = None
        if identity_raw is not None:
            try:
                identity = parse_identity(identity_raw)
            except Exception:
                identity = identity_raw
        return run_action(
            "search",
            path=path,
            query=arguments.get("query", ""),
            limits=limits,
            from_byte=_int_arg(arguments, "from_byte") or 0,
            identity=identity,
        )
    if name == "fetch_excerpt":
        identity_raw = arguments.get("identity")
        try:
            identity = parse_identity(identity_raw)
        except Exception:
            identity = identity_raw
        limits = Limits(
            max_chars=_int_arg(arguments, "max_chars") or DEFAULT_FETCH_CHARS,
            max_lines=HARD_MAX_LINES,
        )
        return run_action(
            "fetch",
            path=path,
            identity=identity,
            byte_offset=_int_arg(arguments, "byte_offset"),
            char_offset=_int_arg(arguments, "char_offset"),
            line=_int_arg(arguments, "line"),
            limits=limits,
        )
    return {"ok": False, "error": {"code": "unknown_tool", "message": "unknown tool"}}


def handle_rpc(message: dict[str, Any]) -> dict[str, Any] | None:
    if message.get("error") and "method" not in message:
        return message
    method = message.get("method")
    msg_id = message.get("id")
    if not isinstance(method, str):
        if msg_id is None:
            return None
        return {"jsonrpc": "2.0", "id": msg_id, "error": {"code": -32600, "message": "Invalid Request"}}
    if method.startswith("notifications/") or method == "notifications/initialized":
        return None
    if method == "initialize":
        client_version = ""
        params = message.get("params") or {}
        if isinstance(params, dict):
            client_version = str(params.get("protocolVersion") or "")
        version = client_version if client_version.startswith("2024-") or client_version.startswith("2025-") else PROTOCOL_VERSION
        return {
            "jsonrpc": "2.0",
            "id": msg_id,
            "result": {
                "protocolVersion": version,
                "capabilities": {"tools": {"listChanged": False}},
                "serverInfo": {"name": SERVER_NAME, "version": __version__},
            },
        }
    if method == "tools/list":
        return {"jsonrpc": "2.0", "id": msg_id, "result": {"tools": _tool_schemas()}}
    if method == "tools/call":
        params = message.get("params") or {}
        if not isinstance(params, dict):
            return {"jsonrpc": "2.0", "id": msg_id, "error": {"code": -32602, "message": "Invalid params"}}
        name = params.get("name")
        arguments = params.get("arguments") or {}
        try:
            result = _handle_tool(str(name), arguments)
            limits = result.get("limits") or {}
            max_output = int(limits.get("max_output_bytes") or 16384)
            payload = dumps_result(result, max_output)
        except (ValueError, TypeError, UnicodeError, OSError):
            payload = dumps_result(
                {
                    "ok": False,
                    "error": {"code": "invalid_params", "message": "arguments are invalid"},
                },
                16384,
            )
        is_error = not result_ok(payload)
        return {
            "jsonrpc": "2.0",
            "id": msg_id,
            "result": {
                "content": [{"type": "text", "text": payload}],
                "isError": is_error,
            },
        }
    if method == "ping":
        return {"jsonrpc": "2.0", "id": msg_id, "result": {}}
    return {"jsonrpc": "2.0", "id": msg_id, "error": {"code": -32601, "message": "Method not found"}}


def serve(stdin: BinaryIO | None = None, stdout: BinaryIO | None = None) -> int:
    in_buf = stdin or sys.stdin.buffer
    out_buf = stdout or sys.stdout.buffer
    try:
        while True:
            message = _read_message(in_buf)
            if message is None:
                return 0
            try:
                reply = handle_rpc(message)
            except (TypeError, ValueError, UnicodeError, OSError):
                msg_id = message.get("id") if isinstance(message, dict) else None
                reply = {
                    "jsonrpc": "2.0",
                    "id": msg_id,
                    "error": {"code": -32603, "message": "Internal error"},
                }
            if reply is not None:
                _write_message(out_buf, reply)
    except BrokenPipeError:
        return 0
    except OSError:
        return 1


def main() -> int:
    return serve()


if __name__ == "__main__":
    raise SystemExit(main())
