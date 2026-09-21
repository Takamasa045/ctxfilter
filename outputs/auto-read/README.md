# ctxfilter 自動読み取り入口

既存ctxfilterに MCP `read_file` と CLI `read` を追加し、共通AGENTSの読み取り指示を接続済みです。人が毎回ツールを選ぶ必要はなく、Codexがこの入口を使うと、サイズと返却予算から決定的に分岐します。全ツール応答を横取りする機能ではありません。

## 使い方

```bash
/opt/homebrew/opt/python@3.14/bin/python3.14 /Users/takamasa/Documents/Codex/2026-09-20/codex-context-filter-grok46/outputs/bin/ctxfilter read --path FILE
# リテラル検索
/opt/homebrew/opt/python@3.14/bin/python3.14 /Users/takamasa/Documents/Codex/2026-09-20/codex-context-filter-grok46/outputs/bin/ctxfilter read --path FILE --query TEXT
```

MCP再読込後は `read_file({"path":"..."})`、検索は `query` を付けます。

- `route=full` / `reason=small_and_fits`: `text` が全文。位置・identity等の付加情報は省略。
- `route=preview`: サイズ超過、JSON出力予算、binary、走査予算に応じて既存preview。追加取得は `fetch_excerpt` にidentityとnext_byte。
- `route=search` / `reason=literal_query`: 既存literal search。未完走なら一致なしと断定せず、identityと `from_byte=next_search_byte` で続ける。

全文候補4096 bytes、本文JSON上限4096 bytesが既定。閾値は `--small-bytes` / MCP `small_bytes`（0〜16384）、返却上限は `--max-output-bytes` / MCP `max_output_bytes`（256〜16384）。範囲外はclamp。JSON化後の実バイト数で判定します。MCP封筒・改行・ツール定義は上限と別です。`max_chars/max_lines` は抜粋にだけ適用します。

通常のサイズ判定・読み取り・検索はJev呼出しゼロ。意味分類は既存ctxfilter-jevの別経路で、明示意図・allowlist・外部送信承認を要する契約を維持します。

## 実測

ローカルの実stdio MCPプロセスにinitialize/list/callを送り、実ファイルから得た応答を計数しました。ネイティブCodex通信のキャプチャではありません。

| 入力・目的 | 自動経路 | 本文JSON | MCP封筒 | 呼び出し |
|---|---|---:|---:|---:|
| 825 B fixture全文 | full | 996 B | 1,255 B | 1、追加0 |
| 655,836 Bログの先頭確認 | preview | 3,049 B | 3,275 B | 1、追加0 |
| 同ログの中間マーカー検索 | search | 1,244 B | 1,445 B | 1 |
| マーカー位置から100文字取得 | fetch | 1,108 B | 1,302 B | 追加1 |

大きいログの先頭経路は2,000 bytesの本文を返し、未完走のcursorを保持。中間マーカーの検索→fetchは計2回、封筒2,747 Bで原文一致を確認。全文読了や意味分類と同じ作業量ではありません。

825 Bの旧preview(400文字)→fetchは今回再計測で3,417 B／2回。過去の正本 `results-live.json` は3,419 B／2回、直接全文は1,315 B／1回。今回の自動入口は1,255 B／1回です。ツール定義の追加1,143 B（tools/listの差）を別途計上しています。旧`results.json`は使用していません。

**これはバイト計測です。Codexトークン・キャッシュ・利用枠・費用の削減率は測定していません。**

## 確認結果

- ctxfilter全46件成功（既存37件＋追加9件）。UTF-8/空/閾値/escape後の予算/巨大一行/変更・置換/権限/binary/検索継続/CLI・MCPの実プロセスを確認。
- ctxfilter-jev連携41件成功。連携テストの固定「37件」だけを「37件以上＋成功終了」に変更し、追加テストを許容。実Jev推論は実行していません。
- ソケットと子プロセス生成を拒否した状態でもfull/preview/searchが動作。新入口に外部通信依存なし。
- 独立コードレビューで初期指摘1件（復元後の再適用）を修正。再レビューで修正必須指摘なし。
- 既存テスト由来のResourceWarningは変更前にも存在。失敗はありません。

## 設定・再読込

`~/.codex/AGENTS.md` のCTXFILTERブロックだけ更新済み。直前バックアップと他ブロック不変を確認。`~/.codex/config.toml` は変更せず、`codex mcp get ctxfilter --json`で既存起動パスが有効か確認済みです。

**このセッションのネイティブツール一覧は旧3ツールのままです。新規read_fileのネイティブ利用は未確認。** 新しいセッションまたはMCP再読込後にread_fileの出現を確認し、上の825 B fixtureと大きなログでfull/previewを各1回確認してください。それまではCLI readが利用できます。AGENTS方針のロードとMCPサーバー再起動は別です。

## 戻し方

指示ブロックの復元は既定dry-run。現ブロックが適用時と一致しない場合は拒否し、他の編集を保存します。実適用には環境の書き込み承認が必要な場合があります。

```bash
/opt/homebrew/opt/python@3.14/bin/python3.14 /Users/takamasa/Documents/Codex/2026-09-21/ctxfilter-auto-read/outputs/registration.py rollback
# 表示された対象ブロックの復元を実施する場合
/opt/homebrew/opt/python@3.14/bin/python3.14 /Users/takamasa/Documents/Codex/2026-09-21/ctxfilter-auto-read/outputs/registration.py rollback --apply
```

MCP設定は変更していないため復元不要。実装を戻す場合は `changes.diff` と本タスク `work/backup/src`、`work/backup/tests`、`work/backup/README.md`、`work/backup/test_original_ctxfilter.py` を比較し、以後の編集がない箇所だけ復元してください。新規auto.py/test_auto_read.pyを含め、削除操作は本作業では行っていません。

実装先: `/Users/takamasa/Documents/Codex/2026-09-20/codex-context-filter-grok46/outputs/src/ctxfilter/`。

リポジトリ内の証拠: [measurement.json](measurement.json)、[tests.txt](tests.txt)、[integration-tests.txt](integration-tests.txt)、[review.md](review.md)。MCP transcript、changes.diff、復元スクリプトとbackupは元のローカル作業ディレクトリに保存されています。
