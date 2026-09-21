#!/usr/bin/env python3
"""Create non-secret local fixtures under work/fixtures."""

from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parent


def _place(buf: bytearray, offset: int, token: bytes) -> None:
    buf[offset : offset + len(token)] = token


def _token_bytes(size: int, offset: int, token: bytes) -> bytes:
    buf = bytearray(b"x" * size)
    _place(buf, offset, token)
    return bytes(buf)


def build() -> dict[str, str]:
    ROOT.mkdir(parents=True, exist_ok=True)
    paths: dict[str, Path] = {}

    empty = ROOT / "empty.txt"
    empty.write_bytes(b"")
    paths["empty"] = empty

    jp_lines = [
        "これは日本語の試験です。",
        "MARKER-JP-START 始め",
        "あいうえお " * 40,
        "絵文字🌻と結合文字がぁ。",
        "MARKER-JP-MID 中頃",
        "漢字交じりの長い行：" + "検索対象。" * 80,
        "MARKER-JP-END 終わり",
    ]
    jp = ROOT / "japanese.txt"
    jp.write_text("\n".join(jp_lines) + "\n", encoding="utf-8")
    paths["japanese"] = jp

    log_lines: list[str] = []
    n = 8000
    for i in range(n):
        if i == 0:
            log_lines.append("MARKER-START unique-aaa-000")
        elif i == n // 2:
            log_lines.append("MARKER-MID unique-bbb-4000")
        elif i == n - 1:
            log_lines.append("MARKER-END unique-ccc-7999")
        else:
            log_lines.append(f"2026-09-20 INFO line={i:04d} filler={'.' * 48}")
    long_log = ROOT / "long-log.txt"
    long_log.write_text("\n".join(log_lines) + "\n", encoding="utf-8")
    paths["long_log"] = long_log

    mid = "MIDTOKEN-XYZ"
    long_line = ("A" * 20000) + mid + ("B" * 20000)
    long_line_path = ROOT / "long-line.txt"
    long_line_path.write_text(long_line + "\n", encoding="utf-8")
    paths["long_line"] = long_line_path

    no_match = ROOT / "no-match.txt"
    no_match.write_text("alpha beta gamma\nwithout the needle\n", encoding="utf-8")
    paths["no_match"] = no_match

    binary = ROOT / "binary.bin"
    binary.write_bytes(bytes(range(256)) + b"\x00payload\x00" + b"\xff" * 64)
    paths["binary"] = binary

    changed = ROOT / "changed.txt"
    changed.write_text("version-one-contents\n", encoding="utf-8")
    paths["changed"] = changed

    mixed = ROOT / "mixed-ascii-jp.txt"
    mixed.write_text("HEAD ascii 日本語TAIL query対象 end\n", encoding="utf-8")
    paths["mixed"] = mixed

    quotes = ROOT / "quotes-heavy.txt"
    quotes.write_text(('\\"quoted\\" ' * 400) + ("日本語 " * 80) + "END-QUOTE-MARK\n", encoding="utf-8")
    paths["quotes"] = quotes

    invalid = ROOT / "invalid-utf8.bin"
    invalid.write_bytes(b"HEAD" + bytes([0xFF]) + b"\nTAIL-ASCII\n" + "日本語".encode("utf-8") + b"\n")
    paths["invalid_utf8"] = invalid

    cases = {
        "b1024_end": (2048, 1024 - 8, b"ENDCHUNK"),
        "b1024_span": (2048, 1020, b"SPANMARK"),
        "b1024_at": (2048, 1024, b"ATCHUNK!"),
        "b1024_after": (2048, 1025, b"AFTER___"),
        "b1024_jp_end": (2048, 1024 - len("日本語B".encode("utf-8")), "日本語B".encode("utf-8")),
        "b1024_jp_span": (2048, 1020, "日本語A".encode("utf-8")),
        "b1024_jp_at": (2048, 1024, "日本語C".encode("utf-8")),
        "b65536_end": (65700, 65536 - 8, b"END65536"),
        "b65536_span": (65700, 65532, b"SPN65536"),
        "b65536_jp_span": (65700, 65532, "日本語S".encode("utf-8")),
        "eof_1024": (1024, 1024 - 8, b"EOFMARK!"),
        "eof_65536": (65536, 65536 - 8, b"EOF65536"),
    }
    for name, (size, offset, token) in cases.items():
        path = ROOT / f"{name}.bin"
        path.write_bytes(_token_bytes(size, offset, token))
        paths[name] = path

    late = ROOT / "late-hit.txt"
    late.write_text(("n" * 500) + "LATEHIT" + ("n" * 500), encoding="utf-8")
    paths["late_hit"] = late

    return {key: str(value) for key, value in paths.items()}


if __name__ == "__main__":
    created = build()
    for key, path in created.items():
        print(f"{key}\t{path}")
