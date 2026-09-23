# ADR 0012: 古い pull request base の事実と明示的な refresh

[English](../../en/decisions/0012-stale-pull-request-base.md) | [日本語](0012-stale-pull-request-base.md)

## ステータス

採用

## コンテキスト

GitHub は pull request に記録した base commit を、対象 branch の現 tip より後ろに残すことがあります。inventory と提出 PR 一覧の差分が膨らみ、AI review が誤ったスコープ逸脱を出します。inventory は読み取り専用（ADR 0006）であり、GitHub を mutate してはなりません。

## 決定

薄い pull-request 事実に、GraphQL の `baseRefOid` と `baseRef.target.oid` から `base_ref_oid`、`base_ref_tip_oid`、`base_ref_stale` を載せます。`flybridge inventory` と `flybridge prs screen` はこれらを報告するだけで PATCH しません。更新は `flybridge prs refresh-base <owner/repo> <number>` です。記録済みの `baseRefName` を再度送り、GitHub に現 tip への再計算を依頼します。

## 結果

- stale base の検知は事実であり、merge 判定でも AI review の分類でもありません。
- refresh は明示的な mutate コマンドです。夜間の inventory は読み取り専用のままです。
- operator skill が、bot のスコープ逸脱コメントをコード作業にする前に refresh するかを決めます。
