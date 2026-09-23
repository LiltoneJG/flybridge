# ADR 0006: 読み取り専用の Orca worktree inventory

[English](../../en/decisions/0006-read-only-worktree-inventory.md) | [日本語](0006-read-only-worktree-inventory.md)

## ステータス

採用

## コンテキスト

operator と agent には、すべての Orca worktree の git 状態および GitHub pull request の事実を示す安定した snapshot が必要です。`orca-ide` と `gh` を shell から呼ぶ場当たり的な Python は session ごとに分岐します。workflow start はローカル checkout の有無で issue を選びません。それらの事実は、専用の読み取り専用 inventory command が提供しなければなりません。

## 決定

読み取り専用の事実 snapshot として `flybridge inventory` を追加します。Orca worktree を列挙し、各 checkout を git で調査し、必要に応じて親および submodule の GitHub repository ごとに pull request の query をまとめます。open query は一括 path のままです。一致しない head には `OPEN`/`MERGED`/`CLOSED` の bounded query を追加し、Orca が hint した pull-request number は state を問わず取得します。`--with-review-facts` はマッチした適格な open pull request への任意の二段目 GraphQL であり、一括 open query は変えません。このコマンドは SQLite の workflow state を書き込まず、agent を開始せず、merge 可否の判定を出力しません。

GitHub adapter の method は引き続き repository と branch head を key とします。それらの事実と Orca path の結合は application/CLI の use case であり、Project screening でも workflow dispatch でもありません。submodule checkout は親 worktree 行の事実であり、追加の Orca worktree ではありません。

## 結果

- agent は inventory JSON を情報収集の source of truth として扱えます。GitHub が有効なとき、選択 repository の query 失敗は JSON を出したうえで終了 status 2 なので、空の `pull_requests` を成功と誤読できません。`github.skip_repositories` の prefix は問い合わせません。1 回リトライ後の HTTP 502/503/504 は `warnings` であり exit status 2 にしません。薄い行は `base_ref_stale` を含みます。GitHub base の更新はこのコマンドではなく `prs refresh-base` です。
- `board screen` は Project issue を列挙します。正規 issue URL によるローカル照合はこのコマンドではなく `board screen --with-refs` です（ADR 0007 を参照）。
- GitHub Actor の `is_bot`（`Bot` 型、App の `resourcePath`、または `[bot]` login）は報告する事実です。User を AI reviewer とみなす、review comment を blocker と解釈するといった operator 方針は、このコマンドの外に残ります。
