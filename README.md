# ctxfilter

**AIエージェントに、ローカルファイルの必要な部分だけを渡す読み取り専用ツールです。** CLI（ターミナル）とMCP（AIエージェントから呼び出す仕組み）の両方で使えます。

Read-only, bounded local file reading through CLI and stdio MCP. No network calls or Jev inference.

## 何ができる？

長いログやソースコードを一度に全部渡す代わりに、まず短く読み、必要に応じて検索・追加取得できます。

- **小さいファイル**：返却上限に収まれば全文を返します。
- **大きいファイル**：先頭の短い抜粋を返し、続きは位置を指定して取得できます。
- **探す言葉が決まっている場合**：指定した文字列をそのまま検索します。正規表現やAIによる意味検索ではありません。

ファイルの書き換えや外部通信は行いません。Python標準ライブラリだけで動き、APIキーは不要です。ただし、返された内容をAIサービスへ渡すかどうかは、呼び出すクライアント側の扱いによります。

## まず使ってみる

Python 3を用意し、リポジトリを取得します。開発時の検証環境はPython 3.14です。

```bash
git clone https://github.com/Takamasa045/ctxfilter.git
cd ctxfilter

# READMEを読む（長ければ短い抜粋になります）
python3 outputs/bin/ctxfilter read --path README.md

# READMEから「MCP」を検索する
python3 outputs/bin/ctxfilter read --path README.md --query MCP
```

`--path` を読みたいファイルのパスに変えて使います。結果はJSONで返ります。

| 結果の項目 | 意味 |
| --- | --- |
| `route` | 全文なら `full`、抜粋なら `preview`、検索なら `search` |
| `reason` | その読み取り方法が選ばれた理由 |
| `text` | `route=full` のときの全文 |
| `identity` と継続位置 | 抜粋・検索の続きを同じファイルから取得するための情報 |

既定では「4096バイト以下」を全文の候補にし、返す本文JSONも4096バイト以内に収めます。JSONの付加情報があるため、4096バイト以下のファイルでも抜粋になる場合があります。MCPの通信形式が加える情報は、この上限とは別です。

検索が途中までのときは `scan_complete=false` になります。この状態を「一致する箇所がない」と判断せず、`identity` と `next_search_byte` を使って検索を続けます。詳細は[使い方と返却形式](outputs/README.md)を参照してください。

## AIエージェントから使う

MCPクライアントの起動コマンドにPythonの実行ファイル、引数にこのリポジトリ内の `outputs/bin/ctxfilter` の絶対パスと `mcp` を指定します。

```bash
python3 outputs/bin/ctxfilter mcp
```

このコマンドは標準入出力でMCPの要求を待ちます。登録後にクライアントを再読み込みし、`read_file` が表示されることを確認してください。既存の `preview_file`、`search_file`、`fetch_excerpt` も使えます。

## ctxfilter-jevとの違い

| | ctxfilter（このリポジトリ） | [ctxfilter-jev](https://github.com/Takamasa045/ctxfilter-jev) |
| --- | --- | --- |
| 役割 | ファイルの読み取り・文字列検索 | JSONLの各記録が目的に関係するかの判定 |
| Jevへの送信 | なし | 実判定では許可された抜粋などを送信 |
| 向いている用途 | 内容の確認、既知の語の検索 | 文面の意味に基づく関連分類 |

## 検証と詳しい資料

リポジトリのルートで実行します。

```bash
PYTHONPATH=outputs/src python3 -m unittest discover -s outputs/tests -v
```

- [使い方と返却形式](outputs/README.md)
- [自動読み取りの検証記録](outputs/auto-read/README.md)
- [計測結果](outputs/auto-read/measurement.json)

詳細資料の絶対パスやMCP登録状況は、開発時の環境・時点の記録です。別の環境ではパスを読み替えてください。通常のUTF-8テキストが主な対象で、不正なUTF-8やバイナリには制約があります。文字数・バイト数の削減は、トークン数・利用枠・料金の削減を保証するものではありません。

認証情報、ローカルのバックアップ、生成済みfixture、実行ログはGit管理から除外しています。テスト用の自作fixture生成スクリプトは含まれます。
