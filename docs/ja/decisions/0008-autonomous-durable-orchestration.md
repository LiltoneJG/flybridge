# ADR 0008: 自律的で永続的な orchestration と state-backed artifact

[English](../../en/decisions/0008-autonomous-durable-orchestration.md) | [日本語](0008-autonomous-durable-orchestration.md)

## ステータス

採用

## コンテキスト

従来の orchestrated lifecycle では、operator が各 handoff を転記し、現在の role を完了して successor を起動する必要がありました。この方式では operator が壊れやすい scheduler となり、plan 文書が実装 repository に残り、review feedback の循環を永続的に表現できません。また Orca worktree ID は runtime repository identity と checkout path を組み合わせるため、それを GitHub の実装 repository と同一視すると、attach した worktree と child worktree の identity が曖昧になります。

## 決定

- orchestrated start は永続 coordinator terminal を1つ作ります。role は `workflow role-ready` で readiness を通知し、coordinator が参照 artifact と repository state を検証して role を完了し、次の role を起動します。
- manager plan、worker verification、reviewer report は、`state_dir/artifacts` 配下のサイズ制限付き UTF-8 artifact とします。file 名は SHA-256 に基づき、private permission を使い、readiness の消費前に検証します。実装 repository に plan commit や coordination file を作りません。
- `changes-requested` review は review artifact とともに既存 worker へ戻します。次の readiness には新しい commit が必要です。この循環は `orchestration.max_review_cycles` で制限します。
- すべての role は、verified/unverified scope を必須 artifact に記録した後、`--outcome blocked` を報告できます。readiness は `blocked_reason` を reviewer approval semantics と分離して保持します。coordinator はその role を閉じ、requested successor を cancel し、blocker consume と aggregate run の blocked 化を同一 transaction で行います。
- workflow record は GitHub implementation repository、Orca runtime repository ID、開始時 Git SHA を別々に保持します。child 作成では runtime repository を選び、identity 検証では implementation repository と commit ancestry を保護します。
- coordinator は terminal run の結果、reviewer readiness の消費、terminal ownership の解放を同一 transaction で永続化します。その後、結果 JSON を書き出して flush してから自身の terminal を閉じます。Real Orca の検証により terminal create は自然に disconnect せず connected shell へ戻るため、flush 後の self-close が必要です。close が失敗して inactive tab が残った場合は operator cleanup を fallback とします。
- 一時的な coordinator error は run に永続化し、process restart をまたいで上限付き exponential backoff で再試行します。永続的な validation error、dead role、review cycle 上限、設定された coordinator error 上限は durable blocked/failed outcome とします。
- hot upgrade または coordinator crash の後、operator は `workflow coordinator-retry` を使えます。live な stale handle を閉じ、replay 可能な blocked/failed run だけを CAS reset し、coordinator 固有の error/release metadata を消去して現行 code を起動します。role、readiness、artifact record は変更しません。completed run、consume 済み blocker、consume 済み review cycle 上限 outcome は reopen できません。未 consume blocker は upgraded code が正しく terminal 処理できるよう replay 可能です。
- `workflow proceed`、`handoff`、`advance` は復旧・診断用 control として残します。通常の orchestrated lifecycle では使いません。

## 初期 state schema

schema version 1 が v1.0.0 の state 形式です。repository identity、開始時 Git SHA、永続 workflow artifact、orchestration run、role-readiness event、review cycle state、coordinator release metadata、nullable な readiness `blocked_reason` を含みます。先行 schema は存在しないため、v1.0.0 に migration path はありません。

## 結果

通常の manager-to-worker-to-reviewer 経路で operator の `proceed` は不要です。進行は CLI process の再起動をまたいで保持され、readiness の replay は冪等です。state storage は機密性のある plan や review evidence を含み得るため、private permission、size limit、digest verification、backup、retention が運用要件になります。
