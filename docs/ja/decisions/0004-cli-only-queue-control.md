# ADR 0004: CLI のみによるリソース queue 制御

[English](../../en/decisions/0004-cli-only-queue-control.md) | [日本語](0004-cli-only-queue-control.md)

## ステータス

採用

## コンテキスト

Orca worktree 内の agent は、短命な stdio MCP client に到達できないことが多いです。resource の acquire、inspect、release、status、cancel、stale recovery は、それらの agent が実際に実行できる単一の操作面を共有しなければなりません。

## 決定

統合 CLI をリソース queue の唯一の操作面とします。agent と operator は `flybridge queue acquire`、`inspect`、`release`、`status`、`cancel`、`recover` を使用します。`queue.resources` が空でない場合、role prompt に `--config` および `--owner` とともにこれらのコマンドを挿入します。MCP package や `flybridge mcp` コマンドは存在しません。

## 結果

- queue の順序は引き続き SQLite ベースで、すべての CLI caller が共有します。
- `queue.resources` は prompt 挿入用の一覧であり、MCP allowlist ではありません。
- agent は、ワークフロー開始時と同じ JSONC を使って Flybridge CLI を実行できる必要があります。
- operator 専用の cancel と stale recovery は、明示的な CLI 操作として残ります。
