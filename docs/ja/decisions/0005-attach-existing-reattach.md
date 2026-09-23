# ADR 0005: アダプター参照を明示的に reconcile した後の再 attach

[English](../../en/decisions/0005-attach-existing-reattach.md) | [日本語](0005-attach-existing-reattach.md)

## ステータス

採用

## コンテキスト

`workflow start --attach-existing` は Orca worktree を作成または削除してはなりません。部分 unique index `workflow_owned_adapter_reference` は、reconcile されていない2行が同じ adapter reference を共有することを引き続き禁止します。cancel、fail、complete の後、Flybridge が checkout を所有していない場合でも、cleanup が実行されるまでこの index が新しい attach を阻止していました。

実行中の attach でも以前の objective を保持して resume prompt を送っていましたが、これは同じパスに新しい指示を出す operator の意図と一致しませんでした。

## 決定

ownership unique index は変更しません。新しい root で adapter reference を再利用できるのは、以前の未 reconcile terminal 行に、worktree を削除せず `external_reconciled_at` を設定した後だけです。

- `running` の owner は同じ workflow ID を維持し、記録された objective を置き換え、その role と mode の start prompt で代替 agent を開始します。
- 所有権を持たない `cancelled` または `failed` workflow は、finish 完了時に reconcile 済みとなり、次の attach は cleanup を待ちません。
- 所有する worktree は cleanup まで未 reconcile のままとし、後の `worktree rm` が、その checkout をまだ必要とする operator と競合しないようにします。

workflow 名が一意なのは `requested`、`starting`、`running` の間だけです。

## 結果

- operator は attach した agent を cancel し、同じパスへ直ちに再 attach できます。`-n` の再利用も可能です。
- Flybridge 所有の worktree を削除する経路は cleanup だけです。
- attach の既定値（`single`、observer 無効）は新しい root に適用され、既存の role と mode を維持する実行中の置換には適用されません。
