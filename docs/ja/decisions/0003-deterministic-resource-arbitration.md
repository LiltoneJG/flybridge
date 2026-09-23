# ADR 0003: SQLite ベースの FIFO リソース調停

[English](../../en/decisions/0003-deterministic-resource-arbitration.md) | [日本語](0003-deterministic-resource-arbitration.md)

## ステータス

採用

## コンテキスト

一部のワークフロー操作は互いに干渉します。agent の会話から実行資格を推測すると、順序、デバッグ、復旧が予測不能になります。

## 決定

名前付きの排他的リソースを SQLite ベースの FIFO queue で表現します。queue はトランザクションによる遷移を使い、event log を出力します。表示可能な Orca terminal がその log を監視し、昇格後に待機中の owner へ通知できますが、スケジューリングを決定したり FIFO 順を変更したりはしません。

## 結果

- 順序と昇格を LLM や Orca なしでテストできます。
- 互いに干渉する任意の操作が名前付きリソースを利用できます。
- stale lease には復旧ルールが必要であり、それは core domain の一部です。operator は明示的に recover でき、durable supervisor は owner が死んだ lease を expire します（ADR 0009）。
- agent と operator は queue に対して単一の CLI 操作面を共有します（ADR 0004 を参照）。
