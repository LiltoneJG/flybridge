# ADR 0008: 統合stateとcommand駆動reconcile

## Status

Accepted.

## Decision

FlybridgeはprivateかつWAL-backedな`flybridge.sqlite3`へ永続stateを保存し、schema version 1から開始します。runはdependency付きstepを持ち、worktree、repository、external reference、agent run、operation、handoff、terminal、FIFO requestを関連付けます。旧databaseはarchive/remove案内とともに拒否し、自動移行・削除しません。

daemonは追加しません。workflow commandは軽量なOrca全件scanを行い、`flybridge reconcile`が明示的な広いscanを行います。外部呼び出し完了後にapply transactionを開始します。Orca statusは観測値でありworkflow判断のauthorityではありません。成功かつ非truncateの全件scanだけを欠落証拠とし、既定cancel閾値は2回かつ300秒です。step cancel、successor cancel、queue解放、FIFO昇格は同時にcommitします。

schemaは同一roleの複数slot・attempt、多対多handoff、agent/model snapshotを許容します。今回のreleaseは `orca.agents.reviewer` を `{agent, model}` の配列として受け取り、worker の固定 commit SHA から fan-out し、全 review を待って全員 `approved` のときだけ完了します。いずれかの `changes-requested` は全 review artifact を fan-in し、上限付きの修正 round を追加します。
