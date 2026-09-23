# ADR 0007: worktree 結合 key としての正規 GitHub issue URL

[English](../../en/decisions/0007-canonical-issue-url.md) | [日本語](0007-canonical-issue-url.md)

## ステータス

採用

## コンテキスト

operator は GitHub Project issue をローカル Orca worktree および development ref と結合する必要があります。path 名の heuristic は repository 間で衝突します。Orca の `linkedIssue` は repository を伴わない番号です。`board screen` は Project field だけを列挙し、`set_lifecycle` は worktree comment を上書きしていました。

## 決定

結合 key は単一 URL ではなく集合です。正規 GitHub issue URL `https://github.com/{owner}/{repo}/issues/{number}` に加え、GitHub development ref（`repository#head_ref_name` と linked branch の `repository+name`）を使います。新しい `workflow start` には `--issue` が必要です。attach-existing は worktree comment から URL を読み取れます。Flybridge はその URL を Orca comment に書き込み、lifecycle comment の更新時にも保持します。`inventory` は comment および同一 repository の `linkedIssue` だけから、URL ベースの `github_hint.issues` を報告します。`board screen --with-refs` は、その集合が worktree の comment URL、親または submodule の origin と branch、inventory が付けた pull-request head と交差すれば結合します。照合ではディレクトリ名を解析せず、GitHub Project field を変更しません。submodule repository を偽の worktree 行としては出しません。

## 結果

- 未着手かどうかの分類は caller のポリシーに残ります。
- comment URL のない worktree は、URL または GitHub development ref が board issue と交差するまで `unmatched_worktrees` に出ます。
- workflow run の参照は統合 SQLite schema（schema version 1）に正規 URL を保存し、互換 workflow facade も `issue_url` を維持します。
