# ctxfilter

ローカルの1ファイルを、短い抜粋と位置指定の追加取得だけで読む読み取り専用ツールです。標準ライブラリの Python 3 のみです。チャット書き換え、全ツール応答の横取り、任意コマンド実行、外部送信、意味分類 LLM は実装していません。

文字数は Unicode のコードポイントです。トークン推定、キャッシュ、利用枠、請求とは別です。このツールは UTF-8 テキストと、NUL を含む binary だけを扱います。他エンコーディングは対象外です。

## 自動読み取り入口（2026-09-21追加）

既定入口は MCP `read_file` / CLI `read`。4096 bytes以下のUTF-8は、本文JSONも4096 bytes以内なら簡潔な全文を返します。サイズ判定はstat/fstatで行い、大きい本文の先読みはしません。サイズ/出力予算を超えた場合は既存preview、`query`があれば既存literal searchへ進みます。通常経路でJevを呼びません。

```bash
/opt/homebrew/opt/python@3.14/bin/python3.14 /Users/takamasa/Documents/Codex/2026-09-20/codex-context-filter-grok46/outputs/bin/ctxfilter read --path FILE
# 検索意図
/opt/homebrew/opt/python@3.14/bin/python3.14 /Users/takamasa/Documents/Codex/2026-09-20/codex-context-filter-grok46/outputs/bin/ctxfilter read --path FILE --query MARKER
```

`small_bytes` / `--small-bytes` は0〜16384、`max_output_bytes` / `--max-output-bytes` は256〜16384に制限。全文の既定候補4096 bytesはJSON付加情報の分だけさらに縮む場合があります。`max_chars` / `max_lines` は抜粋経路に適用。返却上限は本文JSONだけで、MCP封筒と改行は別です。

`route=full` は `text` が全文。`route=preview/search` は既存identity/cursor/EOF契約を維持し、`reason` が選択理由です。返却予算不足でidentity等も収まらない場合は短い `output_budget` エラー。NUL含有はbinary preview、不正UTF-8はエラーです。stat変更とパスの置換を読み取り後に検査します。同一size/mtimeに偽装された変更を保証する仕組みではありません。

MCP登録の起動パスは変更不要。共通AGENTSのCTXFILTERブロックを新入口へ更新済み。46件のctxfilterテストと連携41件が成功。既存3ツールのネイティブ利用は本作業で確認済みですが、新規read_fileは現在のツール一覧への反映待ちです。再読込まではCLI readを使用してください。

実測・運用・復元: `/Users/takamasa/Documents/Codex/2026-09-21/ctxfilter-auto-read/outputs/README.md`。以下の検証表は2026-09-20時点の記録です。

## 使い方

```bash
PYTHONPATH=outputs/src /opt/homebrew/opt/python@3.14/bin/python3.14 outputs/bin/ctxfilter preview --path work/fixtures/long-log.txt --max-chars 400 --max-lines 12
PYTHONPATH=outputs/src /opt/homebrew/opt/python@3.14/bin/python3.14 outputs/bin/ctxfilter search --path work/fixtures/long-log.txt --query MARKER-MID --chunk-size 1024
PYTHONPATH=outputs/src /opt/homebrew/opt/python@3.14/bin/python3.14 outputs/bin/ctxfilter fetch --path FILE --identity-json '{...}' --byte-offset N
PYTHONPATH=outputs/src /opt/homebrew/opt/python@3.14/bin/python3.14 outputs/bin/ctxfilter search --path FILE --query INFO --from-byte N --identity-json '{...}'
PYTHONPATH=outputs/src /opt/homebrew/opt/python@3.14/bin/python3.14 outputs/bin/ctxfilter mcp
```

`mcp` は改行区切り JSON-RPC（NDJSON）です。ツール注釈は `readOnlyHint=true`, `destructiveHint=false` です。

## 既定の上限

| 項目 | 既定 | 硬上限 |
| --- | --- | --- |
| preview 文字 | 2000 | 8000 |
| preview 行 | 40 | 200 |
| fetch 文字 | 4000 | 8000 |
| 一致件数 | 20 | 50 |
| 走査バイト | 8 MiB | 8 MiB |
| 実行時間 | 2000 ms | 2000 ms |
| 応答 JSON | 16384 バイト | 16384 バイト |

`truncated` は返した excerpt が、この要求の開始位置から先の残り全部ではないことです。`scan_complete` はこの要求の開始位置から先が EOF まで完了したことです（ファイル先頭からの全読了ではありません。search の `from_byte` と同じ範囲です）。後続ページや途中からの fetch でも EOF に達したら `scan_complete=true`、`truncated=false`、`stop_reason=eof` です。このとき追加取得を止めます。出力予算で短くした場合は `output_truncated=true` かつ `scan_complete=false` で、`next_byte` は返した excerpt の直後です。1件の一致も入らない予算は `ok=false` / `output_budget` です。CLI の終了コードと MCP の `isError` は短縮後の JSON に合わせます。予算を上げるには CLI `--max-output-bytes` です。

検索の継続は `--from-byte` に前回の `next_search_byte` と identity を渡します。`bytes_scanned` と `next_search_byte` は別です。走査未完了を一致なしとみなしません。8MiB 以降の一致も継続で取れます。

## ファイル読み取りの制約

- 通常ファイルのみ。ディレクトリ、FIFO、デバイスは開きません。非ブロック open と `fstat` を使います。
- 不正 UTF-8 のテキスト preview/fetch は `invalid_utf8` で拒否します。byte 検索はできます。NUL を含む先頭は `binary` の hex です。
- identity の `size` / `mtime_ns` / `inode` / `dev` は十進文字列です。JS の JSON.parse/stringify が 2^53-1 を超える整数を丸めて stale_identity になるのを防ぐためです。受け取り側は文字列のまま渡し、ツールが整数に戻して比較します。
- `identity.sample_sha256` は頭尾 4096 バイトとサイズのサンプルです。全ファイル hash ではありません。同じサイズと mtime のまま中間だけ変えた場合は検知しません。
- 無効パスは長くエコーしません。
- シェル組み立て、eval、正規表現検索はありません。検索は UTF-8 リテラル一致だけです。

## 登録

`~/.codex/config.toml` に `[mcp_servers.ctxfilter]` のみ追加済み。`~/.codex/AGENTS.md` に `BEGIN CTXFILTER` ブロックのみ追加済み。他 MCP / Jev / browser routing / security は未変更。

起動:

- command: `/opt/homebrew/opt/python@3.14/bin/python3.14`
- args: `.../outputs/bin/ctxfilter mcp`

戻し: `outputs/registration/rollback-ctxfilter.py`（デフォルト dry-run）。適用はしていません。backup は `work/backup/`（outputs には置いていません）。

## 検証の状態

| 項目 | 状態 |
| --- | --- |
| CLI 実プロセス | 確認済み。long-log 655836 文字に対し preview 応答 1369 文字。中間マーカーは search→fetch で原文スライス一致 |
| MCP initialize / tools/list / tools/call（NDJSON） | 確認済み |
| unittest | 37 件 OK（Node JSON.parse/stringify 経由の CLI/MCP fetch、EOF 停止を含む） |
| `codex mcp get ctxfilter` | 設定読込を確認 |
| このセッションの Codex ネイティブツール一覧 | 未確認（再読込みが必要） |
| quota 削減の実測 | 未確認 |

詳細は `outputs/verification/`、`outputs/design.md`、`outputs/completion-report.md`。
