# ADR 0010: 設定の GitHub login と提出 pull request の screening

[English](../../en/decisions/0010-github-login-and-authored-prs.md) | [日本語](0010-github-login-and-authored-prs.md)

## Status

Accepted

## Context

`github.login` は `board screen` の既定 assignee にしか使われていませんでした。GitHub adapter はホストの active アカウントで `gh` を呼びます。別アカウントが active だと private repository が解決できません。`flybridge inventory` は Orca worktree の head 照合であり、作者で pull request を列挙できません。agent は場当たり的な `gh search` collector を書いていました。

`gh` にコマンド単位の `--user` はありません。`gh auth switch` はホスト設定を書き換え、他プロセスと競合します。

## Decision

GitHub が有効なとき、adapter はプロセスあたり 1 回 `gh auth token --user <github.login>` を解決し、以降の `gh` 子プロセス環境に `GH_TOKEN` を載せます。ホストの active アカウントは切り替えません。親の `GH_TOKEN` があっても上書きし、呼び出し shell ではなく設定がアカウントを選びます。token は出力しません。`flybridge doctor` は設定 login と active login を報告し、設定 login が認証できなければ失敗します。

提出 pull request の読み取り専用一覧として `flybridge prs screen` を追加します。GitHub search を使い、`--author` の既定は `github.login`、`--state` の既定は `OPEN` です。`--with-review-facts` は inventory と同じ適格条件です。worktree 結合も merge 判定も出しません。inventory は ADR 0006 の worktree snapshot のまま、薄い行に `author` を含めます。

## Consequences

- agent は提出 pull request の収集について `flybridge prs screen` を source of truth とします。そのコマンドが失敗したとき以外、使い捨て collector を書いてはなりません。
- operator はホスト既定の `gh` アカウントを `github.login` と別にできます。
- token 解決には、ホストで `github.login` が `gh auth` 済みである必要があります。
