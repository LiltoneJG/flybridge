# ADR 0009: run 単位の delivery と supervisor timeout

[English](../../en/decisions/0009-run-delivery-and-watchdog-timeouts.md) | [日本語](0009-run-delivery-and-watchdog-timeouts.md)

## Status

Accepted

## Context

orchestrated role は push 禁止で、親 operator が全 reviewer 承認後に harvest と push していたため、人間が全 worktree を回るまで delivery が遅れた。queue wait は `role-ready --outcome blocked` として申告され、run が終端して request が cancel された。child 名 `{name}-worker` の再利用で未 reconcile の ghost が再起動を阻害した。lease の stale recovery は operator 専用で、wait の expire は無かった。

## Decision

- reviewer 全員が同一 SHA を approve したあと、coordinator が manager worktree へ harvest し、`delivery-check` のうえ fast-forward のみ 1 回 push する。force push は非対応。hosted CI は待たない。worker / reviewer は push しない。single mode の agent はローカル検証後に自身が push する。
- queue wait は駐車である。role は request-id を報告して terminal に残る。supervisor は `waiting_resource` とし、waiting / leased の request を持つ role の declared blocker は消費しない。healthy waiter を待ち時間では expire しない。
- `queue.wait_timeout_seconds` は既存 JSONC 互換のため残し、supervisor は使わない。`queue.lease_timeout_seconds` の既定は 3600 秒で、operator の `queue recover` が年齢に使う。supervisor が lease を expire するのは owner が running でない、または terminal が invalid なときに限る。その後 FIFO の次を promote する。`timeouts.role_seconds` の既定は 3600 秒で、queue にいない・未消費の `role-ready` もない running role にだけ適用する。grant と release は `activated_at` を更新する。agent は poll しない。commit は残し、timeout した role を cancel して orchestrated run を timeout 理由付き `blocked` にする。
- orchestrated child 名は workflow-id 8 文字を含む。ghost（adapter あり、worktree 欠落、terminal 終端）は起動前に reconcile する。生きている worktree は削除しない。

## Consequences

- delivery は親 operator の一括作業ではなく、run 単位の coordinator 責務である。
- blocked / failed run でも、harvest が検証済み commit の fast-forward なら進捗 push を 1 回行ってよい。
- SQLite schema version 2 は `workflows.activated_at` を追加し、version 1 からその場マイグレーションする。
