"""Deterministic, read-only, bounded file excerpt operations.

Character counts in this module are Unicode code points of valid UTF-8.
They are not tokens, cache units, quota, or billing.
"""

from __future__ import annotations

import contextlib
import fcntl
import hashlib
import json
import os
import stat
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, BinaryIO, Iterator, Optional

VERSION = 1

DEFAULT_PREVIEW_CHARS = 2000
DEFAULT_PREVIEW_LINES = 40
DEFAULT_FETCH_CHARS = 4000
DEFAULT_MAX_MATCHES = 20
DEFAULT_MATCH_CONTEXT_CHARS = 120
HARD_MAX_CHARS = 8000
HARD_MAX_MATCHES = 50
HARD_MAX_LINES = 200
MAX_OUTPUT_BYTES = 16_384
MAX_SCAN_BYTES = 8_388_608
MAX_RUNTIME_MS = 2000
MAX_PATH_DISPLAY = 180
MAX_ERROR_DETAIL = 120
MAX_PATTERN_CHARS = 200
MAX_ABS_PATH_CHARS = 4096
CHUNK_SIZE = 65_536
BINARY_PROBE_BYTES = 8192
SAMPLE_WINDOW = 4096
MAX_QUERY_BYTES = 800


class ToolError(Exception):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code
        self.message = message


@dataclass(frozen=True)
class Limits:
    max_chars: int = DEFAULT_PREVIEW_CHARS
    max_lines: int = DEFAULT_PREVIEW_LINES
    max_matches: int = DEFAULT_MAX_MATCHES
    match_context_chars: int = DEFAULT_MATCH_CONTEXT_CHARS
    max_scan_bytes: int = MAX_SCAN_BYTES
    max_runtime_ms: int = MAX_RUNTIME_MS
    max_output_bytes: int = MAX_OUTPUT_BYTES
    chunk_size: int = CHUNK_SIZE
    hard_max_chars: int = HARD_MAX_CHARS

    def clamp(self) -> "Limits":
        return Limits(
            max_chars=_clamp(self.max_chars, 1, self.hard_max_chars),
            max_lines=_clamp(self.max_lines, 1, HARD_MAX_LINES),
            max_matches=_clamp(self.max_matches, 1, HARD_MAX_MATCHES),
            match_context_chars=_clamp(self.match_context_chars, 0, 400),
            max_scan_bytes=_clamp(self.max_scan_bytes, 64, MAX_SCAN_BYTES),
            max_runtime_ms=_clamp(self.max_runtime_ms, 10, MAX_RUNTIME_MS),
            max_output_bytes=_clamp(self.max_output_bytes, 256, MAX_OUTPUT_BYTES),
            chunk_size=_clamp(self.chunk_size, 64, CHUNK_SIZE),
            hard_max_chars=_clamp(self.hard_max_chars, 1, HARD_MAX_CHARS),
        )


@dataclass(frozen=True)
class Identity:
    path: str
    size: int
    mtime_ns: int
    inode: int
    dev: int
    sample_sha256: str

    def as_dict(self) -> dict[str, Any]:
        # Decimal strings so JS JSON.parse/stringify does not round past 2^53-1.
        return {
            "path": self.path,
            "size": str(self.size),
            "mtime_ns": str(self.mtime_ns),
            "inode": str(self.inode),
            "dev": str(self.dev),
            "sample_sha256": self.sample_sha256,
        }


class Deadline:
    def __init__(self, runtime_ms: int):
        self.end = time.monotonic() + (runtime_ms / 1000.0)

    def expired(self) -> bool:
        return time.monotonic() >= self.end


def _clamp(value: int, lo: int, hi: int) -> int:
    return max(lo, min(hi, int(value)))


def path_display(path: object, limit: int = MAX_PATH_DISPLAY) -> str:
    if not isinstance(path, str):
        return "<invalid-path>"
    if len(path) <= limit:
        return path
    keep = max(8, (limit - 3) // 2)
    return path[:keep] + "..." + path[-keep:]


def error_envelope(tool: str, code: str, message: str, path: str | None = None) -> dict[str, Any]:
    msg = message if len(message) <= MAX_ERROR_DETAIL else message[: MAX_ERROR_DETAIL - 3] + "..."
    out: dict[str, Any] = {
        "ok": False,
        "v": VERSION,
        "tool": tool,
        "error": {"code": code, "message": msg},
    }
    if path:
        out["path_display"] = path_display(path)
    return out


def _parse_id_int(value: Any) -> int:
    if isinstance(value, bool) or isinstance(value, float):
        raise ToolError("invalid_identity", "identity numeric fields are invalid")
    if isinstance(value, int):
        return value
    if isinstance(value, str) and value.isdigit():
        return int(value)
    raise ToolError("invalid_identity", "identity numeric fields are invalid")


def parse_identity(raw: Any) -> Identity:
    if not isinstance(raw, dict):
        raise ToolError("invalid_identity", "identity must be an object")
    required = ("path", "size", "mtime_ns", "inode", "dev", "sample_sha256")
    for key in required:
        if key not in raw:
            raise ToolError("invalid_identity", "identity is missing a required field")
    path = raw["path"]
    if not isinstance(path, str) or not path or "\x00" in path:
        raise ToolError("invalid_identity", "identity path is invalid")
    if len(path) > MAX_ABS_PATH_CHARS:
        raise ToolError("invalid_identity", "identity path exceeds length limit")
    try:
        size = _parse_id_int(raw["size"])
        mtime_ns = _parse_id_int(raw["mtime_ns"])
        inode = _parse_id_int(raw["inode"])
        dev = _parse_id_int(raw["dev"])
    except ToolError:
        raise
    except (TypeError, ValueError):
        raise ToolError("invalid_identity", "identity numeric fields are invalid")
    sample = raw["sample_sha256"]
    if not isinstance(sample, str) or len(sample) > 64:
        raise ToolError("invalid_identity", "identity sample hash is invalid")
    if size < 0 or mtime_ns < 0 or inode < 0 or dev < 0:
        raise ToolError("invalid_identity", "identity numeric fields are invalid")
    return Identity(
        path=path,
        size=size,
        mtime_ns=mtime_ns,
        inode=inode,
        dev=dev,
        sample_sha256=sample,
    )


def resolve_regular_file(user_path: str) -> Path:
    if not isinstance(user_path, str) or not user_path or "\x00" in user_path:
        raise ToolError("invalid_path", "path is invalid")
    if len(user_path) > MAX_ABS_PATH_CHARS:
        raise ToolError("path_too_long", "path exceeds length limit")
    path = Path(user_path)
    if not path.is_absolute():
        path = Path.cwd() / path
    try:
        resolved = Path(os.path.realpath(path))
    except OSError:
        raise ToolError("invalid_path", "path could not be resolved")
    if len(str(resolved)) > MAX_ABS_PATH_CHARS:
        raise ToolError("path_too_long", "resolved path exceeds length limit")
    try:
        st = os.stat(resolved, follow_symlinks=True)
    except FileNotFoundError:
        raise ToolError("not_found", "file not found")
    except PermissionError:
        raise ToolError("permission_denied", "permission denied")
    except OSError:
        raise ToolError("invalid_path", "path could not be read")
    mode = st.st_mode
    if stat.S_ISDIR(mode):
        raise ToolError("is_directory", "path is a directory")
    if not stat.S_ISREG(mode):
        raise ToolError("not_a_regular_file", "path is not a regular file")
    return resolved


def open_regular(path: Path) -> tuple[BinaryIO, os.stat_result]:
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0)
    flags |= getattr(os, "O_NOFOLLOW", 0)
    flags |= getattr(os, "O_NONBLOCK", 0)
    fd = -1
    try:
        fd = os.open(str(path), flags)
        st = os.fstat(fd)
        if stat.S_ISDIR(st.st_mode):
            raise ToolError("is_directory", "path is a directory")
        if not stat.S_ISREG(st.st_mode):
            raise ToolError("not_a_regular_file", "path is not a regular file")
        current = fcntl.fcntl(fd, fcntl.F_GETFL)
        fcntl.fcntl(fd, fcntl.F_SETFL, current & ~os.O_NONBLOCK)
        handle = os.fdopen(fd, "rb")
        fd = -1
        return handle, st
    except ToolError:
        if fd >= 0:
            os.close(fd)
        raise
    except FileNotFoundError:
        if fd >= 0:
            os.close(fd)
        raise ToolError("not_found", "file not found")
    except PermissionError:
        if fd >= 0:
            os.close(fd)
        raise ToolError("permission_denied", "permission denied")
    except OSError:
        if fd >= 0:
            os.close(fd)
        raise ToolError("io_error", "file could not be read")


def _stat_fields(st: os.stat_result) -> tuple[int, int, int, int]:
    return (
        int(st.st_size),
        int(getattr(st, "st_mtime_ns", int(st.st_mtime * 1_000_000_000))),
        int(st.st_ino),
        int(st.st_dev),
    )


def _sample_sha256_handle(handle: BinaryIO, size: int) -> str:
    digest = hashlib.sha256()
    digest.update(b"size=")
    digest.update(str(size).encode("ascii"))
    pos = handle.tell()
    handle.seek(0)
    head = handle.read(SAMPLE_WINDOW)
    digest.update(b"\nhead=")
    digest.update(head)
    if size > SAMPLE_WINDOW:
        handle.seek(max(0, size - SAMPLE_WINDOW))
        tail = handle.read(SAMPLE_WINDOW)
        digest.update(b"\ntail=")
        digest.update(tail)
    handle.seek(pos)
    return digest.hexdigest()


def identity_from_handle(handle: BinaryIO, st: os.stat_result, path: Path) -> Identity:
    size, mtime_ns, inode, dev = _stat_fields(st)
    return Identity(
        path=str(path),
        size=size,
        mtime_ns=mtime_ns,
        inode=inode,
        dev=dev,
        sample_sha256=_sample_sha256_handle(handle, size),
    )


def identities_match(expected: Identity, actual: Identity) -> bool:
    return (
        expected.size == actual.size
        and expected.mtime_ns == actual.mtime_ns
        and expected.inode == actual.inode
        and expected.dev == actual.dev
        and expected.sample_sha256 == actual.sample_sha256
        and os.path.normpath(expected.path) == os.path.normpath(actual.path)
    )


def detect_kind_handle(handle: BinaryIO, size: int) -> str:
    if size == 0:
        return "empty"
    pos = handle.tell()
    handle.seek(0)
    probe = handle.read(min(BINARY_PROBE_BYTES, size))
    handle.seek(pos)
    if b"\x00" in probe:
        return "binary"
    return "text"


@contextlib.contextmanager
def regular_file(user_path: str) -> Iterator[tuple[Path, BinaryIO, Identity, str]]:
    resolved = resolve_regular_file(user_path)
    handle, st = open_regular(resolved)
    try:
        ident = identity_from_handle(handle, st, resolved)
        kind = detect_kind_handle(handle, ident.size)
        handle.seek(0)
        yield resolved, handle, ident, kind
        st_after = os.fstat(handle.fileno())
        after = _stat_fields(st_after)
        before = (ident.size, ident.mtime_ns, ident.inode, ident.dev)
        if after != before:
            raise ToolError("changed_during_read", "file changed during read")
    finally:
        handle.close()


def _utf8_len_from_lead(lead: int) -> int:
    if lead < 0x80:
        return 1
    if 0xC2 <= lead <= 0xDF:
        return 2
    if 0xE0 <= lead <= 0xEF:
        return 3
    if 0xF0 <= lead <= 0xF4:
        return 4
    return 1


def align_utf8_start(handle: BinaryIO, offset: int, size: int) -> int:
    if offset <= 0:
        handle.seek(0)
        return 0
    if offset >= size:
        handle.seek(size)
        return size
    handle.seek(offset)
    skipped = 0
    while skipped < 4:
        byte = handle.read(1)
        if not byte:
            return offset + skipped
        if (byte[0] & 0xC0) != 0x80:
            aligned = offset + skipped
            handle.seek(aligned)
            return aligned
        skipped += 1
    handle.seek(offset)
    return offset


def read_codepoint(handle: BinaryIO) -> tuple[Optional[str], bytes, bool]:
    """Read one Unicode scalar or one replacement for an ill-formed byte.

    Invalid UTF-8 never consumes a following ASCII byte or newline. Each
    ill-formed byte becomes U+FFFD. Text preview/fetch then reject the file.
    """
    first = handle.read(1)
    if not first:
        return None, b"", False
    lead = first[0]
    if lead < 0x80:
        return first.decode("ascii"), first, False
    needed = _utf8_len_from_lead(lead)
    if needed == 1:
        return "\ufffd", first, True
    rest = handle.read(needed - 1)
    valid_cont = 0
    for byte in rest:
        if (byte & 0xC0) != 0x80:
            break
        valid_cont += 1
    extra = rest[valid_cont:]
    if extra:
        handle.seek(handle.tell() - len(extra))
    seq = first + rest[:valid_cont]
    if valid_cont != needed - 1:
        return "\ufffd", seq, True
    try:
        ch = seq.decode("utf-8")
    except UnicodeDecodeError:
        return "\ufffd", seq, True
    if len(ch) != 1:
        return "\ufffd", seq, True
    return ch, seq, False


def _hex_preview(data: bytes, n: int = 64) -> str:
    return " ".join(f"{byte:02x}" for byte in data[:n])


def preview_file(path: str, limits: Limits | None = None) -> dict[str, Any]:
    limits = (limits or Limits()).clamp()
    deadline = Deadline(limits.max_runtime_ms)
    with regular_file(path) as (resolved, handle, ident, kind):
        base = {
            "ok": True,
            "v": VERSION,
            "tool": "preview",
            "path_display": path_display(str(resolved)),
            "identity": ident.as_dict(),
            "kind": kind,
            "size_bytes": ident.size,
        }
        if kind == "empty":
            return _finalize(
                {
                    **base,
                    "excerpt": "",
                    "start_byte": 0,
                    "end_byte": 0,
                    "start_char": 0,
                    "end_char": 0,
                    "start_line": 1,
                    "end_line": 1,
                    "returned_chars": 0,
                    "truncated": False,
                    "stop_reason": "eof",
                    "next_byte": 0,
                    "next_char": 0,
                    "next_line": 1,
                    "scan_complete": True,
                    "bytes_scanned": 0,
                    "line_known": True,
                    "char_known": True,
                    "decode_errors": False,
                },
                limits,
            )
        if kind == "binary":
            data = handle.read(min(64, ident.size, limits.max_scan_bytes))
            returned = len(data)
            truncated = returned < ident.size
            return _finalize(
                {
                    **base,
                    "excerpt": "",
                    "hex": _hex_preview(data),
                    "start_byte": 0,
                    "end_byte": returned,
                    "start_char": None,
                    "end_char": None,
                    "start_line": None,
                    "end_line": None,
                    "returned_chars": 0,
                    "returned_bytes": returned,
                    "truncated": truncated,
                    "stop_reason": "binary" if truncated else "eof",
                    "next_byte": returned,
                    "next_char": None,
                    "next_line": None,
                    "scan_complete": not truncated,
                    "bytes_scanned": returned,
                    "line_known": False,
                    "char_known": False,
                    "decode_errors": False,
                },
                limits,
            )
        return _read_text_window(
            handle,
            ident,
            path=resolved,
            kind=kind,
            tool="preview",
            start_byte=0,
            skip_chars=0,
            skip_lines=0,
            start_char=0,
            start_line=1,
            limits=limits,
            deadline=deadline,
            line_known=True,
            char_known=True,
            count_from_start=True,
        )


def fetch_excerpt(
    path: str,
    identity: Identity | dict[str, Any],
    *,
    byte_offset: int | None = None,
    char_offset: int | None = None,
    line: int | None = None,
    limits: Limits | None = None,
) -> dict[str, Any]:
    limits = (limits or Limits(max_chars=DEFAULT_FETCH_CHARS, max_lines=HARD_MAX_LINES)).clamp()
    deadline = Deadline(limits.max_runtime_ms)
    specified = [item is not None for item in (byte_offset, char_offset, line)]
    if sum(specified) != 1:
        raise ToolError("invalid_offset", "exactly one of byte_offset, char_offset, line is required")
    expected = identity if isinstance(identity, Identity) else parse_identity(identity)
    with regular_file(path) as (resolved, handle, actual, kind):
        if not identities_match(expected, actual):
            raise ToolError("stale_identity", "file identity changed; preview or search again")
        if kind == "binary":
            if byte_offset is None:
                raise ToolError("invalid_offset", "binary fetch requires byte_offset")
            return _fetch_binary(handle, resolved, actual, byte_offset, limits, deadline)
        if byte_offset is not None:
            if byte_offset < 0:
                raise ToolError("invalid_offset", "byte_offset is invalid")
            if byte_offset > actual.size:
                raise ToolError("invalid_offset", "byte_offset is past end of file")
            return _read_text_window(
                handle,
                actual,
                path=resolved,
                kind=kind,
                tool="fetch",
                start_byte=byte_offset,
                skip_chars=0,
                skip_lines=0,
                start_char=None,
                start_line=None,
                limits=limits,
                deadline=deadline,
                line_known=False,
                char_known=False,
                count_from_start=False,
            )
        if char_offset is not None:
            if char_offset < 0:
                raise ToolError("invalid_offset", "char_offset is invalid")
            return _read_text_window(
                handle,
                actual,
                path=resolved,
                kind=kind,
                tool="fetch",
                start_byte=0,
                skip_chars=char_offset,
                skip_lines=0,
                start_char=char_offset,
                start_line=1,
                limits=limits,
                deadline=deadline,
                line_known=True,
                char_known=True,
                count_from_start=True,
            )
        assert line is not None
        if line < 1:
            raise ToolError("invalid_offset", "line must be >= 1")
        return _read_text_window(
            handle,
            actual,
            path=resolved,
            kind=kind,
            tool="fetch",
            start_byte=0,
            skip_chars=0,
            skip_lines=line - 1,
            start_char=0,
            start_line=line,
            limits=limits,
            deadline=deadline,
            line_known=True,
            char_known=True,
            count_from_start=True,
        )


def _fetch_binary(
    handle: BinaryIO,
    path: Path,
    ident: Identity,
    byte_offset: int,
    limits: Limits,
    deadline: Deadline,
) -> dict[str, Any]:
    if byte_offset < 0 or byte_offset > ident.size:
        raise ToolError("invalid_offset", "byte_offset is invalid")
    n = min(64, ident.size - byte_offset, limits.max_scan_bytes)
    if deadline.expired():
        raise ToolError("timeout", "time limit reached before fetch")
    handle.seek(byte_offset)
    data = handle.read(n)
    end = byte_offset + len(data)
    truncated = end < ident.size
    return _finalize(
        {
            "ok": True,
            "v": VERSION,
            "tool": "fetch",
            "path_display": path_display(str(path)),
            "identity": ident.as_dict(),
            "kind": "binary",
            "size_bytes": ident.size,
            "excerpt": "",
            "hex": _hex_preview(data),
            "start_byte": byte_offset,
            "end_byte": end,
            "start_char": None,
            "end_char": None,
            "start_line": None,
            "end_line": None,
            "returned_chars": 0,
            "returned_bytes": len(data),
            "truncated": truncated,
            "stop_reason": "binary" if truncated else "eof",
            "next_byte": end,
            "next_char": None,
            "next_line": None,
            "scan_complete": not truncated,
            "bytes_scanned": len(data),
            "line_known": False,
            "char_known": False,
            "requested_byte": byte_offset,
            "decode_errors": False,
        },
        limits,
    )


def _read_text_window(
    handle: BinaryIO,
    ident: Identity,
    *,
    path: Path,
    kind: str,
    tool: str,
    start_byte: int,
    skip_chars: int,
    skip_lines: int,
    start_char: int | None,
    start_line: int | None,
    limits: Limits,
    deadline: Deadline,
    line_known: bool,
    char_known: bool,
    count_from_start: bool,
) -> dict[str, Any]:
    out_chars: list[str] = []
    bytes_scanned = 0
    chars_seen = 0
    lines_skipped = 0
    emitted_lines = 0
    emit_started = False
    aligned = start_byte
    actual_start_byte = start_byte
    actual_start_char = start_char
    actual_start_line = start_line
    current_line = 1 if count_from_start else None
    current_char = 0 if count_from_start else None
    stop_reason = None
    reached_offset = skip_chars == 0 and skip_lines == 0
    aligned = align_utf8_start(handle, start_byte, ident.size)
    actual_start_byte = aligned
    if skip_chars == 0 and skip_lines == 0:
        emit_started = True
        reached_offset = True
    while True:
        if deadline.expired():
            stop_reason = "timeout"
            break
        if bytes_scanned >= limits.max_scan_bytes:
            stop_reason = "max_scan_bytes"
            break
        ch, seq, bad = read_codepoint(handle)
        if ch is None:
            stop_reason = "eof"
            break
        if bad:
            raise ToolError("invalid_utf8", "file is not valid UTF-8")
        bytes_scanned += len(seq)
        if not emit_started:
            if current_char is not None:
                current_char += 1
            if ch == "\n":
                if current_line is not None:
                    current_line += 1
                lines_skipped += 1
            chars_seen += 1
            if skip_lines and lines_skipped >= skip_lines and skip_chars == 0:
                emit_started = True
                reached_offset = True
                actual_start_byte = aligned + bytes_scanned
                actual_start_line = current_line
                actual_start_char = current_char
                continue
            if skip_chars and chars_seen >= skip_chars and skip_lines == 0:
                emit_started = True
                reached_offset = True
                actual_start_byte = aligned + bytes_scanned
                actual_start_char = current_char
                actual_start_line = current_line
                continue
            continue
        if len(out_chars) >= limits.max_chars:
            handle.seek(handle.tell() - len(seq))
            bytes_scanned -= len(seq)
            stop_reason = "max_chars"
            break
        out_chars.append(ch)
        if current_char is not None:
            current_char += 1
        if ch == "\n":
            emitted_lines += 1
            if current_line is not None:
                current_line += 1
            if emitted_lines >= limits.max_lines:
                stop_reason = "max_lines"
                break

    if not reached_offset and stop_reason in {"timeout", "max_scan_bytes", "eof"}:
        code = "offset_not_reached" if stop_reason != "timeout" else "timeout"
        raise ToolError(code, "requested location was not reached within scan limits")

    text = "".join(out_chars)
    if skip_chars == 0 and skip_lines == 0:
        end_byte = aligned + bytes_scanned
        actual_start_byte = aligned
    else:
        end_byte = actual_start_byte + len(text.encode("utf-8"))

    returned_chars = len(text)
    eof_reached = stop_reason == "eof"
    truncated = not eof_reached
    if line_known and actual_start_line is not None:
        next_line = actual_start_line + text.count("\n")
        if text.endswith("\n") and stop_reason == "max_lines":
            next_line = actual_start_line + emitted_lines
    else:
        next_line = None
    if char_known and actual_start_char is not None:
        next_char = actual_start_char + returned_chars
    else:
        next_char = None

    return _finalize(
        {
            "ok": True,
            "v": VERSION,
            "tool": tool,
            "path_display": path_display(str(path)),
            "identity": ident.as_dict(),
            "kind": kind if kind != "empty" else "text",
            "size_bytes": ident.size,
            "excerpt": text,
            "start_byte": actual_start_byte,
            "end_byte": end_byte,
            "start_char": actual_start_char if char_known else None,
            "end_char": (actual_start_char + returned_chars) if char_known and actual_start_char is not None else None,
            "start_line": actual_start_line if line_known else None,
            "end_line": next_line if line_known else None,
            "returned_chars": returned_chars,
            "returned_bytes": end_byte - actual_start_byte,
            "truncated": truncated and ident.size > 0,
            "stop_reason": stop_reason or "eof",
            "next_byte": end_byte,
            "next_char": next_char,
            "next_line": next_line,
            "scan_complete": bool(eof_reached),
            "bytes_scanned": bytes_scanned if count_from_start else (end_byte - actual_start_byte),
            "line_known": line_known,
            "char_known": char_known,
            "requested_byte": start_byte if tool == "fetch" else 0,
            "aligned_byte": aligned if tool == "fetch" else 0,
            "decode_errors": False,
        },
        limits,
    )


def utf8_start_byte_count(data: bytes) -> int:
    return sum((byte & 0xC0) != 0x80 for byte in data)


def search_file(
    path: str,
    query: str,
    limits: Limits | None = None,
    *,
    from_byte: int = 0,
    identity: Identity | dict[str, Any] | None = None,
) -> dict[str, Any]:
    limits = (limits or Limits()).clamp()
    deadline = Deadline(limits.max_runtime_ms)
    if not isinstance(query, str) or query == "" or "\x00" in query:
        raise ToolError("invalid_query", "query is invalid")
    if len(query) > MAX_PATTERN_CHARS:
        raise ToolError("invalid_query", "query exceeds length limit")
    try:
        needle = query.encode("utf-8")
    except UnicodeEncodeError:
        raise ToolError("invalid_query", "query is not valid Unicode")
    if len(needle) > MAX_QUERY_BYTES:
        raise ToolError("invalid_query", "query exceeds byte length limit")
    if from_byte < 0:
        raise ToolError("invalid_offset", "from_byte is invalid")

    with regular_file(path) as (resolved, handle, ident, kind):
        if identity is not None:
            expected = identity if isinstance(identity, Identity) else parse_identity(identity)
            if not identities_match(expected, ident):
                raise ToolError("stale_identity", "file identity changed; preview or search again")
        if from_byte > ident.size:
            raise ToolError("invalid_offset", "from_byte is past end of file")

        base = {
            "ok": True,
            "v": VERSION,
            "tool": "search",
            "path_display": path_display(str(resolved)),
            "identity": ident.as_dict(),
            "kind": kind,
            "size_bytes": ident.size,
            "query_mode": "literal",
            "from_byte": from_byte,
        }
        if ident.size == 0 or from_byte == ident.size:
            return _finalize(
                {
                    **base,
                    "matches": [],
                    "matches_returned": 0,
                    "match_limit": limits.max_matches,
                    "truncated_matches": False,
                    "scan_complete": True,
                    "bytes_scanned": 0,
                    "searched_end_byte": ident.size,
                    "next_search_byte": ident.size,
                    "stop_reason": "eof",
                    "positions_from": "file_start" if from_byte == 0 else "unknown",
                },
                limits,
            )

        matches: list[dict[str, Any]] = []
        bytes_scanned = 0
        scan_complete = False
        stop_reason = "eof"
        truncated_matches = False
        overlap = len(needle) - 1
        carry = b""
        file_offset = from_byte
        handle.seek(from_byte)
        total_newlines = 0
        total_start_bytes = 0
        searched_end_byte = from_byte
        positions_from = "file_start" if from_byte == 0 else "unknown"

        while True:
            if deadline.expired():
                stop_reason = "timeout"
                break
            if bytes_scanned >= limits.max_scan_bytes:
                stop_reason = "max_scan_bytes"
                break
            to_read = min(limits.chunk_size, limits.max_scan_bytes - bytes_scanned)
            chunk = handle.read(to_read)
            if not chunk:
                scan_complete = True
                stop_reason = "eof"
                searched_end_byte = ident.size
                break
            data = carry + chunk
            data_start = file_offset - len(carry)
            newlines_before_data = total_newlines - carry.count(b"\n")
            start_bytes_before_data = total_start_bytes - utf8_start_byte_count(carry)
            start = 0
            hit_match_cap = False
            while len(matches) < limits.max_matches:
                idx = data.find(needle, start)
                if idx < 0:
                    break
                byte_offset = data_start + idx
                if byte_offset >= from_byte:
                    prefix = data[:idx]
                    match = {
                        "byte_offset": byte_offset,
                        "match_bytes": len(needle),
                        "excerpt": _match_excerpt(data, idx, len(needle), limits.match_context_chars),
                    }
                    if from_byte == 0:
                        match["char_offset"] = start_bytes_before_data + utf8_start_byte_count(prefix)
                        match["line"] = 1 + newlines_before_data + prefix.count(b"\n")
                    else:
                        match["char_offset"] = None
                        match["line"] = None
                    matches.append(match)
                    if len(matches) >= limits.max_matches:
                        truncated_matches = True
                        stop_reason = "max_matches"
                        searched_end_byte = byte_offset + 1
                        hit_match_cap = True
                        break
                start = idx + 1
            bytes_scanned += len(chunk)
            if hit_match_cap:
                scan_complete = False
                break
            total_newlines += chunk.count(b"\n")
            total_start_bytes += utf8_start_byte_count(chunk)
            file_offset += len(chunk)
            carry = data[-overlap:] if overlap > 0 else b""
            if len(data) >= len(needle):
                searched_end_byte = data_start + len(data) - len(needle) + 1
            else:
                searched_end_byte = data_start
            if len(chunk) < to_read:
                scan_complete = True
                stop_reason = "eof"
                searched_end_byte = ident.size
                break

        if scan_complete and not truncated_matches:
            next_search_byte = ident.size
            searched_end_byte = ident.size
        elif truncated_matches:
            next_search_byte = searched_end_byte
        else:
            next_search_byte = min(ident.size, searched_end_byte)

        return _finalize(
            {
                **base,
                "matches": matches,
                "matches_returned": len(matches),
                "match_limit": limits.max_matches,
                "truncated_matches": truncated_matches,
                "scan_complete": scan_complete and not truncated_matches,
                "bytes_scanned": bytes_scanned,
                "searched_end_byte": searched_end_byte,
                "next_search_byte": next_search_byte,
                "stop_reason": stop_reason,
                "positions_from": positions_from,
            },
            limits,
        )


def _match_excerpt(data: bytes, idx: int, needle_len: int, context_chars: int) -> str:
    match_bytes = data[idx : idx + needle_len]
    match_text = match_bytes.decode("utf-8", errors="replace")
    left_bytes = data[max(0, idx - context_chars * 4) : idx]
    left_text = left_bytes.decode("utf-8", errors="replace")
    if len(left_text) > context_chars:
        left_text = left_text[-context_chars:]
    right_bytes = data[idx + needle_len : idx + needle_len + context_chars * 4]
    right_text = right_bytes.decode("utf-8", errors="replace")
    if len(right_text) > context_chars:
        right_text = right_text[:context_chars]
    return left_text + match_text + right_text


def _sync_cursors_to_excerpt(obj: dict[str, Any]) -> None:
    excerpt = obj.get("excerpt")
    if not isinstance(excerpt, str):
        return
    start_byte = int(obj.get("start_byte") or 0)
    encoded_len = len(excerpt.encode("utf-8"))
    obj["returned_chars"] = len(excerpt)
    obj["returned_bytes"] = encoded_len
    obj["end_byte"] = start_byte + encoded_len
    obj["next_byte"] = obj["end_byte"]
    start_char = obj.get("start_char")
    if isinstance(start_char, int):
        obj["end_char"] = start_char + len(excerpt)
        obj["next_char"] = obj["end_char"]
    start_line = obj.get("start_line")
    if isinstance(start_line, int):
        next_line = start_line + excerpt.count("\n")
        obj["end_line"] = next_line
        obj["next_line"] = next_line


def _output_budget_error(tool: object) -> dict[str, Any]:
    return {
        "ok": False,
        "v": VERSION,
        "tool": tool,
        "error": {
            "code": "output_budget",
            "message": "response exceeded output budget; raise max_output_bytes",
        },
        "output_truncated": True,
        "scan_complete": False,
    }


def dumps_result(obj: dict[str, Any], max_output_bytes: int) -> str:
    def dump(current: dict[str, Any]) -> str:
        return json.dumps(current, ensure_ascii=False, separators=(",", ":"), allow_nan=False)

    def encoded_size(text: str) -> int:
        return len(text.encode("utf-8"))

    try:
        text = dump(obj)
        if encoded_size(text) <= max_output_bytes:
            return text
        shrunk = json.loads(text)
    except (TypeError, ValueError, UnicodeError):
        return json.dumps(_output_budget_error(obj.get("tool")), ensure_ascii=True, separators=(",", ":"))

    shrunk["output_truncated"] = True
    shrunk["truncated"] = True
    shrunk["stop_reason"] = "max_output_bytes"
    shrunk["scan_complete"] = False

    if shrunk.get("tool") == "search" and isinstance(shrunk.get("matches"), list):
        original = list(shrunk["matches"])
        matches = list(original)
        while matches and encoded_size(dump({**shrunk, "matches": matches})) > max_output_bytes:
            matches.pop()
        if original and not matches:
            return dump(_output_budget_error("search"))
        if len(matches) < len(original):
            shrunk["truncated_matches"] = True
            dropped = original[len(matches)]
            shrunk["next_search_byte"] = dropped.get("byte_offset", shrunk.get("next_search_byte"))
        shrunk["matches"] = matches
        shrunk["matches_returned"] = len(matches)
        text = dump(shrunk)
        if encoded_size(text) <= max_output_bytes:
            return text

    excerpt = shrunk.get("excerpt")
    if isinstance(excerpt, str) and excerpt:
        lo, hi = 0, len(excerpt)
        best: dict[str, Any] | None = None
        while lo <= hi:
            mid = (lo + hi) // 2
            candidate = dict(shrunk)
            candidate["excerpt"] = excerpt[:mid]
            _sync_cursors_to_excerpt(candidate)
            encoded = dump(candidate)
            if encoded_size(encoded) <= max_output_bytes:
                best = candidate
                lo = mid + 1
            else:
                hi = mid - 1
        if best is not None and best.get("excerpt"):
            return dump(best)

    return dump(_output_budget_error(obj.get("tool")))


def _finalize(obj: dict[str, Any], limits: Limits) -> dict[str, Any]:
    obj["limits"] = {
        "max_chars": limits.max_chars,
        "max_lines": limits.max_lines,
        "max_matches": limits.max_matches,
        "max_scan_bytes": limits.max_scan_bytes,
        "max_runtime_ms": limits.max_runtime_ms,
        "max_output_bytes": limits.max_output_bytes,
    }
    return obj


def result_ok(payload: str) -> bool:
    try:
        parsed = json.loads(payload)
    except (json.JSONDecodeError, UnicodeError, TypeError):
        return False
    return bool(isinstance(parsed, dict) and parsed.get("ok"))


def run_action(action: str, **kwargs: Any) -> dict[str, Any]:
    path = kwargs.get("path")
    try:
        if action == "preview":
            return preview_file(kwargs["path"], kwargs.get("limits"))
        if action == "search":
            from_byte_raw = kwargs.get("from_byte") or 0
            if not isinstance(from_byte_raw, int) or isinstance(from_byte_raw, bool):
                from_byte_raw = int(from_byte_raw)
            return search_file(
                kwargs["path"],
                kwargs["query"],
                kwargs.get("limits"),
                from_byte=from_byte_raw,
                identity=kwargs.get("identity"),
            )
        if action == "fetch":
            return fetch_excerpt(
                kwargs["path"],
                kwargs["identity"],
                byte_offset=kwargs.get("byte_offset"),
                char_offset=kwargs.get("char_offset"),
                line=kwargs.get("line"),
                limits=kwargs.get("limits"),
            )
        raise ToolError("invalid_action", "unknown action")
    except ToolError as exc:
        return error_envelope(action, exc.code, exc.message, path if isinstance(path, str) else None)
    except (OSError, TypeError, ValueError, UnicodeError):
        return error_envelope(action, "invalid_input", "input is invalid", path if isinstance(path, str) else None)
