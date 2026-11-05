# 限定信頼性試験

workflow の監督・復旧を変更した後は、この手順で手動監督 baseline と候補動作を比較します。生の運用記録は公開しません。

## 記録と実行

入力と出力は ignore 済みの `.reliability-trials/` 配下に置きます。JSON 配列の各 record は `variant`（`baseline` / `trial`）、`scenario`、`work_kind`（`implementation` / `validation` / `review`）を識別し、次の非負指標をすべて持つ必要があります。

- `completion_seconds`、`intervention_count`、`intervention_seconds`
- `detection_seconds`、`recovery_seconds`
- `missing_results`、`stale_running_states`、`orphan_ownership`
- `rework_count`、`resource_wait_seconds`

normal exit、abnormal exit、missing result、question waiting、monitor restart、delayed duplicate result、interrupted cleanup、validation-only work、retained dirty work を両 variant で実行します。両 variant で implementation、validation、review がすべて含まれるよう割り当てます。使い捨ての state と repository を使い、production worktree や credential を試験データにしません。

```console
uv run python scripts/reliability_trial.py \
  .reliability-trials/records.json \
  --output .reliability-trials/summary.json
```

coverage 不足、case identity の重複、不正な指標は拒否されます。出力は集計値だけですが、issue に安全な要約を転記する前に内容を確認します。生 record、command log、repository path、terminal handle、prompt、artifact、生成 summary は commit しません。

## 判断とロールバック

missing result、stale running state、orphan ownership が増えず、全 abnormal scenario が明示的な terminal または attention state に到達し、理由のない intervention count/time の悪化がない場合だけ候補を採用します。completion time と resource wait は診断値であり、validation や cleanup の省略によって改善してはいけません。

安全性指標が非ゼロ、別 activation の ownership を変更、duplicate result が final result を上書き、interrupted cleanup で保持作業を喪失、または必須証跡を収集不能なら手動監督へ戻します。fallback では自動 advance を止め、現在の worktree と state directory を保持し、`workflow status` と queue ownership を確認します。外部 cleanup を確認した後だけ operator が文書化済みの recovery command を実行します。
