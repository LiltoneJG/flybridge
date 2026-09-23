# ADR 0002: JSONC 設定とパスだけで参照する外部 skill

[English](../../en/decisions/0002-configuration-and-external-skills.md) | [日本語](0002-configuration-and-external-skills.md)

## ステータス

採用

## コンテキスト

ワークフローには、個人および組織が所有するルールを、公開したり親 shell に動作を依存させたりせずに適用する必要があります。また、ユーザーには調べやすい設定インターフェースが必要です。

## 決定

`config/` 内の JSONC ファイルを唯一のユーザー設定ソースとします。`default_mode` を上書きするのは `workflow start --mode` と `doctor --mode` だけ、`queue.observer` を上書きするのは `workflow start --queue-observer` と `workflow start --no-queue-observer` だけです。それ以外の CLI 引数は操作への入力であり、設定の上書きではありません。外部 skill のソースと role の割り当ては、そのファイル内の絶対パスまたは glob パターンです。外部 skill のパスでは `~` とディレクトリのシンボリックリンクを解決し、得られた文書を生成する英語 prompt の索引に含めます。

## 結果

- skill の内容、shell 由来のパス、認証情報は commit されません。
- 設定が明示されているため、子 shell は親 shell と同じように動作します。
- 環境変数と XDG 設定探索は意図的にサポートしません。XDG 形式のパスは runtime state には引き続き適切です。
