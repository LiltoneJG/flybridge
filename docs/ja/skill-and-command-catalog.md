# Skill・コマンドカタログ

[English](../en/skill-and-command-catalog.md) | [日本語](skill-and-command-catalog.md)

Flybridge に含めるのは、再利用可能な公開 guidance だけです。組織および個人の policy は外部 catalog に置き、ローカル JSONC 設定から参照します。

## 同梱する guidance

| パス                                    | 目的                                                                                                                                                                                                                                                 |
| --------------------------------------- | ---------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `skills/resource-coordination/SKILL.md` | 名前付き FIFO resource を一度 acquire し、必要なら lease ID の通知を待ち、後片付けの確認まで lease を保持する                                                                                                                                        |
| `skills/role-lifecycle/SKILL.md`        | orchestrated role は artifact を保存して `role-ready` を通知し、queue wait は駐車である。single role は検証後に 1 回 push して停止する。coordinator が承認済み tip を harvest / delivery-check / fast-forward push し、所有する child を retire する |

同梱の `SKILL.md` 文書を検出するには、`skills.sources` に `skills/` の絶対パス、または `/path/to/flybridge/skills/*/SKILL.md` のような glob を指定します。外部 skill の path では `~` とディレクトリのシンボリックリンクを展開します。

## Agent 向け queue コマンド

| コマンド                                                     | 目的                                                                                                                                                                                                                                                                                            |
| ------------------------------------------------------------ | ----------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `flybridge doctor --mode orchestrated`                       | launch 前に state storage、role agent、skill path、必要 command、到達可能な Orca runtime を検証する                                                                                                                                                                                             |
| `flybridge workflow start --attach-existing`                 | 既存の Orca worktree を作成・削除せず、その中で新しい agent を開始する。queue observer は `queue.observer` に従い、`--queue-observer` / `--no-queue-observer` で上書きできる                                                                                                                    |
| `flybridge workflow start --batch`                           | JSON 配列の既存 worktree に順番に attach し、item ごとに JSONL、最後に `{ok, failed, results[]}` を出す。各 item は `path`（または `repository`）、`objective` または `objective_file`、任意の `mode` / `name` / `issue`。observer 規則は `--attach-existing` と同じです                        |
| `flybridge workflow observe <workflow-id>`                   | 生きている owner terminal を持つ実行中 workflow に表示可能な queue observer を開く。死んでいれば先に resume する                                                                                                                                                                                |
| `flybridge workflow cleanup`                                 | 復旧可能な workflow ownership を dry-run 報告する（`--dry-run` と同じ）。残った owned child がある終了 root は `retire_recommended`。`--apply` には `--older-than-seconds` が必要で、`--force-age` は暗黙です                                                                                   |
| `flybridge workflow harvest [--dry-run]`                     | 終端した manager worktree へ worker の commit を取り込む。root / child / run id を複数指定できる。terminal は閉じず worktree も削除しない                                                                                                                                                       |
| `flybridge workflow delivery-check <id>...`                  | manager HEAD が全ローカル reviewer の承認した SHA と一致することを push 前に検証する                                                                                                                                                                                                            |
| `flybridge workflow retire --keep manager\|none [--dry-run]` | harvest のあと、所有する worker / reviewer の terminal と worktree を閉じる。`--keep manager` は manager を残し、`--keep none` は manager terminal も閉じ、Flybridge が所有する manager worktree だけ削除する。attach-existing の manager worktree は削除しない                                 |
| `flybridge workflow artifact put`                            | orchestrated role 所有の bounded plan / verification / review artifact を `state_dir` に原子的に保存する                                                                                                                                                                                        |
| `flybridge workflow artifact show` / `verify`                | durable workflow artifact を読み取る、または digest を検証する                                                                                                                                                                                                                                  |
| `flybridge workflow role-ready`                              | orchestrated role の terminal を閉じず readiness を通知する。reviewer は approved/changes-requested、全 orchestrated role は `--outcome blocked` を指定できる                                                                                                                                   |
| `flybridge workflow status`                                  | workflow と child、orchestration run / review cycle、repository identity、artifact metadata、observer state、読み取り専用の `progress` を報告する                                                                                                                                               |
| `flybridge board screen`                                     | 設定された GitHub Project issue を列挙する。任意で `--board`、`--status`、`--priority`、`--assignee`、`--all-assignees`、`--with-refs`、`--path-prefix`、`--exclude-prefix`、`--exclude-name`。`--status` / `--priority` は空白と大文字小文字を無視する。`--with-refs` 前に同一 checkout を畳む |
| `flybridge prs screen`                                       | 提出者の GitHub pull request を列挙する。任意で `--author`（既定は `github.login`）、繰り返しの `--state`、`--with-review-facts`。薄い行に `base_ref_stale`。worktree とは結合しない                                                                                                            |
| `flybridge prs refresh-base`                                 | pull request の base を PATCH し、対象 branch の現 tip へ再計算させる                                                                                                                                                                                                                           |
| `flybridge inventory`                                        | Orca worktree の git 情報と pull request の snapshot を取得する。`reconcile.exclude_worktrees` と `github.skip_repositories` を適用する。同一 resolved path の Orca カードは 1 行に畳み `orca.aliases` を付ける。一時的な GitHub 5xx は warning であり exit 2 にしない                          |
| `flybridge reconcile [--dry-run] [--no-github]`              | worktree、git、参照の永続観測を reconcile する。dry-run は予定変更だけを返す。既定で `reconcile.exclude_worktrees` を適用する                                                                                                                                                                   |
| `flybridge worktree repository add\|remove`                  | 観測済み Orca worktree の明示 related repository relation を管理する                                                                                                                                                                                                                            |
| `flybridge queue acquire`                                    | 名前付き FIFO queue に一度参加し、request ID または lease ID を受け取る                                                                                                                                                                                                                         |
| `flybridge queue inspect`                                    | operator/debug 用に owner と一致する request を検索する                                                                                                                                                                                                                                         |
| `flybridge queue release`                                    | owner と一致する正確な lease を解放し、最古の waiter を昇格させる                                                                                                                                                                                                                               |
| `flybridge queue status`                                     | queue の集計数を読み取る。`--details` では active request の ID、owner、経過時間、lease の注意表示も報告する                                                                                                                                                                                    |
| `flybridge queue watch --notify-workflow`                    | event を stream し、FIFO 昇格後に lease ID prompt を送る                                                                                                                                                                                                                                        |

`queue.resources` が空でない場合、role prompt に acquire/release が含まれます。待機中の agent は worktree observer が lease ID を通知するまで停止します。inspect や watch を poll してはなりません。resource list が空なら resource の指示は挿入されません。元の agent terminal が無効になると、通知を行う observer は request や lease を変更せず、Flybridge 所有の terminal を閉じます。`workflow observe` は owner terminal が死んでいる間は代替を開きません。`workflow resume` は `queue.observer` が true なら observer を復元します。queue の cancel と stale recovery は意図的に operator 専用の CLI 操作です。

## Operator の案内

| command                    | 用途                                                                    |
| -------------------------- | ----------------------------------------------------------------------- |
| `flybridge operator guide` | 非公開本文を露出せず、設定済み Operator skill の index を検証・出力する |

Operator は workflow role ではなく呼び出し側です。最初に index 内の routing skill を読み、依頼に該当する skill を続けて読みます。workflow 全体の制約は一度だけ判断し、その結論と有効範囲を role に渡します。担当 role が確認すべき事実には issue、pull request、review comment の直接 URL を渡します。必要な検証証拠（コード検査以外の証拠提出や、求められた hosted CI のキックを含む）が不足する間は complete または proceed しません。hosted CI は起動までが完了条件で、結果待ちはしません。不足する前提条件を取得するか role を blocked のままにします。

## 自律 lifecycle と復旧コマンド

| コマンド                               | 目的                                                                                                                               |
| -------------------------------------- | ---------------------------------------------------------------------------------------------------------------------------------- |
| `flybridge workflow supervise`         | durable coordinator loop。通常は orchestrated start が Orca terminal に自動作成し、`--once` は診断と replay test に使う            |
| `flybridge workflow coordinator-retry` | operator hot-upgrade/crash recovery。eligible な blocked/failed run を reset し、role progress を変えず現行 coordinator を起動する |
| `flybridge workflow coordinator-close` | flush 後 self-close が完了しなかった coordinator tab を閉じる operator fallback                                                    |
| `flybridge workflow proceed`           | operator recovery 専用: handoff を記録し、実行中の source role を完了して advance する                                             |
| `flybridge workflow list`              | workflow を列挙し、任意で status により filter する                                                                                |
| `flybridge workflow link\|unlink`      | run または workflow step id の明示 primary/related issue・PR 参照を管理する                                                        |
| `flybridge workflow handoff`           | recovery 専用: source role が `running` または `completed` の間に1行の successor summary を記録する                                |
| `flybridge workflow complete`          | 実行中の role を完了し、所有 terminal を閉じる。orchestrated manager または worker は successor handoff が存在するまで拒否される   |
| `flybridge workflow advance`           | recovery 専用: predecessor が `completed` で handoff が存在するとき、次の child を起動する                                         |

通常の role は artifact を保存して `role-ready` を実行し、coordinator が検証して advance します。manager は repository を開始時 SHA のままにします。worker は cycle 開始 SHA より新しい変更を commit します。reviewer は `approved` または `changes-requested` を記録し、後者は設定された cycle limit まで review artifact とともに既存 worker へ戻ります。role prompt は手動 lifecycle command を禁止します。operator は blocked または中断 run の復旧時だけ、role 外の terminal から実行します。

artifact は実装 repository ではなく、`state_dir/artifacts/workflows/<manager-id>/<kind>.<sha256>.md` に content-addressed 形式で保存します。workflow identity は `implementation_repository`、`runtime_repository_id`、`start_sha` の3つを分離します。runtime ID は attach した root の child に正しい Orca repository を選び、ほかの field は source repository と Git ancestry を検証します。

## Worktree inventory の契約

Orca workspace の merge readiness に関する「事実」が必要な agent は、ほかの Flybridge コマンドと同じ `--config` で `flybridge inventory` を実行しなければなりません。review・comment・thread 本文が必要なら `--with-review-facts` を付けます。収集については、その JSON 文書が source of truth です。`flybridge inventory` 自体が失敗した場合を除き、使い捨ての Python collector や生の `gh api graphql` query を書いてはなりません。JSON 出力のあとの非ゼロ終了は収集失敗であり、merge 可否の判定ではありません。このコマンドは事実だけを報告します。review author の GitHub Actor `is_bot` は出しますが、operator の方針判定はせず、review comment を blocker と解釈せず、pull request が merge 可能かどうかを決定しません。

Project issue をローカル checkout と結合するには、`flybridge board screen --with-refs` を実行します。このコマンドは正規 GitHub issue URL（Orca comment、同一 repository の linked issue、issue body 内の URL）と GitHub development ref（親および submodule の branch head を含む）の集合交差で照合します。どの issue とも交差しない selected checkout は `unmatched_worktrees` に出ます。worktree のディレクトリ名を解析せず、作業が未着手かどうかも決定しません。

提出した pull request の事実が必要な agent は、同じ `--config` で `flybridge prs screen` を実行しなければなりません。review・comment・thread 本文が必要なら `--with-review-facts` を付けます。`flybridge prs screen` 自体が失敗した場合を除き、使い捨ての collector や生の `gh search` を書いてはなりません。worktree 結合も merge 可否の判定も出しません。
