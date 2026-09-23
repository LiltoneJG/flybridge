# ADR 0011: 終了 run の child retire と LFS を避ける submodule prepare

[English](../../en/decisions/0011-terminal-run-child-retirement.md) | [日本語](0011-terminal-run-child-retirement.md)

## Status

Accepted

## Context

orchestrated run が `completed` / `blocked` / `failed` になっても、coordinator は harvest（成功時は push）したあと worker / reviewer worktree を残していました。次の `workflow start --attach-existing` が同じ workflow 名を再利用すると、worktree が残っている限り `AdapterReferenceConflict` になります。name ghost の自動解放は worktree と terminal の両方が消えたときだけです。

`workflow cleanup --dry-run` は active な record と未 reconcile の failed / cancelled worktree だけを出していました。completed の manager に未 reconcile の child が残っていても `candidate_count: 0` になります。

`workflow retire` は manager worktree へ harvest したあと、120 秒制限の `git submodule update --init --recursive --checkout` を実行します。入れ子の Git LFS submodule がこの制限を超えると index が dirty のまま残り、次は `manager worktree is not clean` で失敗します。harvest に LFS blob は不要です。

## Decision

- orchestrated run が `completed` / `blocked` / `failed` / `delivery_failed` になったら、delivery または progress-push のあと coordinator が `retire --keep manager` を実行します。attach-existing の manager worktree は削除しません。retire 失敗は root の `cleanup_error` に記録し、run outcome は巻き戻しません。
- `workflow cleanup --dry-run` は、未 reconcile の owned child worktree が残っている終了済み orchestrated root も列挙し、`retire_recommended` と `root_id` を付けます。`--apply` は従来どおり age 強制の stale reconcile だけです。残った child は `workflow retire --keep manager` で閉じます。
- repository preparation の submodule 更新には `GIT_LFS_SKIP_SMUDGE=1` を載せます。timeout と非ゼロ終了は fail-closed のままです。失敗後に manager が dirty なら、その事実をエラーに含めます。
- operator の `workflow harvest` と `workflow retire --keep manager|none` は復旧用に残します。worker path が git worktree でなくなっているときは harvest を `worker_worktree_gone` で skip し、retire は child を閉じ続けます。実体がある worktree の identity 不一致は fail-closed のままです。

## Consequences

- 同じ名前の夜間 attach が、親 agent の retire 忘れに依存しなくなります。
- 入れ子 LFS checkout が harvest / retire を止めなくなります。
- 自動 retire が失敗したとき、cleanup dry-run が operator retire の合図になります。
- trash に移済みの worker が `retire --keep manager` を止めなくなります。
