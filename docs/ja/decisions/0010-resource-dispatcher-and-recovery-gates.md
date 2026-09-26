# ADR 0010: Resource dispatcher と復旧ゲート

[English](../../en/decisions/0010-resource-dispatcher-and-recovery-gates.md) | [日本語](0010-resource-dispatcher-and-recovery-gates.md)

## 状態

採用

## 背景

SQLite は FIFO の昇格を原子的に処理していましたが、待機 request の昇格時に observer が存在しない場合がありました。prompt の送信は agent の受領や実行を証明しません。また、後片付けが未確認の lease を workflow 終了時に cancel すると、外部 resource に一時状態が残ったまま次の waiter が進み得ました。

## 決定

- 共有 FIFO の権威は SQLite に残します。state directory ごとの dispatcher を OS file lock で1つに制限し、永続通知と登録済み command の実行を担わせます。observer は表示専用です。
- 送信と受領 ACK を別々に記録し、未 ACK の通知は同じ request ID で再送します。
- `queue run` の job は grant 後に一度だけ実行します。実行可能な cleanup check の成功だけを自動解放の証拠とし、不明な実行結果や失敗した check は自動再実行せず復旧待ちにします。
- workflow 終了を含む未確認 lease の cancel は resource を block します。operator が対象 request の外部 cleanup を確認してから FIFO を進めます。経過時間だけの recovery は読み取り専用です。
- workflow start と後続の queue/supervisor command は dispatcher を再起動できます。OS service は登録しないため、全 Flybridge process が停止した後の自動再起動は保証しません。

## 結果

SQLite schema version 4 は version 3 からその場で移行します。manual acquire/release は残し、cleanup 完了の判断は agent が担います。復旧待ち block は自動進行より resource の安全を優先します。
