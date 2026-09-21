"""Command-line interface. User values are never passed to a shell."""

from __future__ import annotations

import argparse
import json
import sys
from typing import Any, Sequence

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
    result_ok,
    run_action,
)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="ctxfilter",
        description="Read-only bounded excerpts from one local file.",
    )
    parser.add_argument("--version", action="version", version=f"ctxfilter {__version__}")
    sub = parser.add_subparsers(dest="command", required=True)

    read = sub.add_parser("read", help="Automatically choose compact full text, preview, or literal search.")
    _add_path(read)
    _add_common_limits(read)
    read.add_argument("--small-bytes", type=int, default=DEFAULT_SMALL_BYTES)
    read.add_argument("--query")
    read.add_argument("--from-byte", type=int, default=0)
    read.add_argument("--identity-json")
    read.add_argument("--max-chars", type=int, default=DEFAULT_PREVIEW_CHARS)
    read.add_argument("--max-lines", type=int, default=DEFAULT_PREVIEW_LINES)
    read.add_argument("--max-matches", type=int, default=DEFAULT_MAX_MATCHES)
    read.set_defaults(max_output_bytes=DEFAULT_OUTPUT_BYTES)

    preview = sub.add_parser("preview", help="Return a short prefix and a fetch cursor.")
    _add_path(preview)
    _add_common_limits(preview)
    preview.add_argument("--max-chars", type=int, default=DEFAULT_PREVIEW_CHARS)
    preview.add_argument("--max-lines", type=int, default=DEFAULT_PREVIEW_LINES)

    search = sub.add_parser("search", help="Literal search with a bounded match list.")
    _add_path(search)
    _add_common_limits(search)
    search.add_argument("--query", required=True)
    search.add_argument("--max-matches", type=int, default=DEFAULT_MAX_MATCHES)
    search.add_argument("--max-chars", type=int, default=DEFAULT_PREVIEW_CHARS)
    search.add_argument("--max-lines", type=int, default=DEFAULT_PREVIEW_LINES)
    search.add_argument("--from-byte", type=int, default=0)
    search.add_argument("--identity-json", default=None, help="Identity from a previous search, for continuation.")

    fetch = sub.add_parser("fetch", help="Return more bytes from a previous cursor.")
    _add_path(fetch)
    _add_common_limits(fetch)
    fetch.add_argument("--identity-json", required=True, help="Identity object from preview/search.")
    loc = fetch.add_mutually_exclusive_group(required=True)
    loc.add_argument("--byte-offset", type=int)
    loc.add_argument("--char-offset", type=int)
    loc.add_argument("--line", type=int)
    fetch.add_argument("--max-chars", type=int, default=DEFAULT_FETCH_CHARS)
    fetch.add_argument("--max-lines", type=int, default=HARD_MAX_LINES)

    sub.add_parser("mcp", help="Run the stdio MCP server (newline-delimited JSON).")
    return parser


def _add_path(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--path", required=True)


def _add_common_limits(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--max-scan-bytes", type=int)
    parser.add_argument("--max-runtime-ms", type=int)
    parser.add_argument("--max-output-bytes", type=int)
    parser.add_argument("--chunk-size", type=int)


def _limits_from_args(args: argparse.Namespace) -> Limits:
    kwargs: dict[str, Any] = {
        "max_chars": getattr(args, "max_chars", DEFAULT_PREVIEW_CHARS),
        "max_lines": getattr(args, "max_lines", DEFAULT_PREVIEW_LINES),
        "max_matches": getattr(args, "max_matches", DEFAULT_MAX_MATCHES),
    }
    if getattr(args, "max_scan_bytes", None) is not None:
        kwargs["max_scan_bytes"] = args.max_scan_bytes
    if getattr(args, "max_runtime_ms", None) is not None:
        kwargs["max_runtime_ms"] = args.max_runtime_ms
    if getattr(args, "max_output_bytes", None) is not None:
        kwargs["max_output_bytes"] = args.max_output_bytes
    if getattr(args, "chunk_size", None) is not None:
        kwargs["chunk_size"] = args.chunk_size
    return Limits(**kwargs).clamp()


def _load_identity(raw: str | None) -> Any:
    if raw is None:
        return None
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        return {"invalid": True}


def dispatch(args: argparse.Namespace) -> tuple[int, str]:
    limits = _limits_from_args(args)
    if args.command == "read":
        result = read_file(args.path, query=args.query, small_bytes=args.small_bytes,
                           limits=limits, from_byte=args.from_byte,
                           identity=_load_identity(args.identity_json))
    elif args.command == "preview":
        result = run_action("preview", path=args.path, limits=limits)
    elif args.command == "search":
        result = run_action(
            "search",
            path=args.path,
            query=args.query,
            limits=limits,
            from_byte=args.from_byte,
            identity=_load_identity(args.identity_json),
        )
    elif args.command == "fetch":
        result = run_action(
            "fetch",
            path=args.path,
            identity=_load_identity(args.identity_json),
            byte_offset=args.byte_offset,
            char_offset=args.char_offset,
            line=args.line,
            limits=limits,
        )
    else:
        result = {"ok": False, "error": {"code": "invalid_action", "message": "unknown command"}}
    payload = dumps_result(result, limits.max_output_bytes)
    code = 0 if result_ok(payload) else 1
    return code, payload


def main(argv: Sequence[str] | None = None) -> int:
    parser = _parser()
    args = parser.parse_args(list(argv) if argv is not None else None)
    if args.command == "mcp":
        from ctxfilter.mcp_server import serve

        return serve()
    exit_code, payload = dispatch(args)
    sys.stdout.write(payload)
    if not payload.endswith("\n"):
        sys.stdout.write("\n")
    return exit_code
