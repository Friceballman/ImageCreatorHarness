# Image Workbench

Codexの会話内で画像を生成・編集し、案件、参照画像、制作要件、候補、レビュー、採用判断、履歴をローカルに記録するハーネスです。仕様の基準は[Notionの仕様書 ver4.1（Codex会話環境版）](https://app.notion.com/p/3f4f252ab68381e8bc40dc251b3ca08c)のver4本文と第14節の追補です。ver3のPDFは過去版として保管し、本仕様と矛盾するAPI要件は適用しません。

このリポジトリは画像生成のための外部APIを呼び出しません。Codexの会話が生成・利用者との確認・フィードバックを担当し、専用CLI `iwb` が生成計画・画像の検証と保存・履歴・レポートを管理します。CLIからCodexの画像生成機能を直接起動することはありません。

## 開発環境

- Python 3.12以上
- `pip install -e .` で実行依存を導入
- `pip install -e ".[dev]"` で開発依存（pytestを含む）を導入

PowerShellでの例：

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -e ".[dev]"
```

このアプリはAPIキー、アクセストークン、`.env`を必要とせず、自動読込もしません。画像生成を使うには、対応するCodex/ChatGPT会話環境とアカウント利用権限が必要です。

## 画像生成と利用枠

生成計画を確認し、Codex会話で個別に明示承認した後、会話内の組み込み画像生成機能を使います。生成結果はCodex会話から利用可能なファイルとして作業領域に保存してから `iwb generate register` または `iwb edit register` で登録します。結果をローカルへ受け渡せない場合、候補登録済みとは扱いません。

会話での承認・停止・失敗も、対応するGenerationの履歴へ記録します。Codexは生成計画提示後に `request-approval` を記録し、利用者の明示承認を受けて `approve` を記録してから画像生成を始めます。生成開始前に上限へ達した場合は `block --reason-code usage_limit_reached` を記録して停止します。生成後に結果を確定できない場合は `mark-unknown` を記録し、確認できるまで再生成しません。生成・編集の各サブコマンドで同じ状態記録を利用します。

組み込み画像生成はCodexの一般利用枠を消費します。利用上限に達したことが分かったら画像生成を停止し、追加クレジット、API、他サービスへ切り替えません。利用枠やクレジットの残量はこのアプリから照会・制御できません。アカウント側の設定によって利用枠到達後にクレジットが使われる可能性があるため、追加クレジットを使わない運用では、アカウント側の支出設定も事前に確認してください。

希望枚数は目安です。実際の出力数と希望数が違っても自動で追加生成せず、実出力数と登録数を記録します。

## データとバックアップ

案件データと画像は `projects/` 以下に保存します。バックアップ対象は `projects/` 全体です。別ドライブ等へ定期的にコピーし、復旧時はアプリを停止してから案件フォルダーを戻してください。Gitだけをバックアップとして扱わないでください。

元画像・参照画像・生成候補・過去の生成計画・履歴は上書き・物理削除しません。`projects/` のGit追跡または除外方針は仕様に従い、Gitから除外する場合は別バックアップが必要です。`.env`、トークン、Pythonキャッシュ、ビルド成果物はGit管理しません。

## CLI

CLIの入口は `iwb` です。全コマンドに `--help` を用意します。

```powershell
iwb --help
iwb project --help
iwb generate --help
iwb generate plan <project-id> --prompt "背景を青空にする"
iwb generate show <generation-id>
iwb generate request-approval <generation-id>
# 会話で利用者が明示承認した後
iwb generate approve <generation-id>
# Codex会話で画像を生成し、出力をローカルへ保存した後
iwb generate register <generation-id> <image-path>
```

案件を使うコマンドでは案件IDを明示します。画像生成そのものと利用者からのフィードバックはCodex会話で行い、候補のレビュー・採用判断は別操作として記録します。

## selected候補の一括出力（ver4.1）

```powershell
iwb project export <project-id>
```

指定案件の `selected` 候補をすべて表示し、確認後に `exports/<export-id>/` へ元の形式でコピーします。案件の採用版1枚に限定する操作ではありません。画像一覧 `manifest.json` から元の案件・生成計画・候補・SHA-256を確認できます。0件なら何も出力せず終了し、再実行時は新しい出力IDを使います。元画像・候補状態・採用版・過去の出力は変更しません。アーカイブ済み案件は出力できません。

既存JSONの変更やデータ移行は不要です。追加仕様は[Notion仕様書のver4.1追補（第14節）](https://app.notion.com/p/3f4f252ab68381e8bc40dc251b3ca08c)を参照してください。

## 一覧キャッシュとエディタ表示（ver4.2）

`project list`・`reference list`・`requirement list`・`candidate list`・`generate list` は、対話端末では実行時点の完全なIDと説明をキャッシュに保存してVS Codeへ表示を依頼します。コマンドは待機せず戻るため、一覧を開いたままコピーして次の操作に使用できます。タブやエディタを閉じる操作は不要で、キャッシュは自動削除しません。

```powershell
iwb project list
iwb reference list <project-id>
iwb requirement list <project-id>
iwb candidate list <project-id> --generation <generation-id>
iwb generate list <project-id>
# エディタを開かず、端末に全文を表示
iwb project list --terminal
```

`generate list` は案件内の生成計画（新規生成・編集）を、完了・失敗・取消等を含めて新しい順に表示します。Generation IDと生成計画IDは同じUUIDです。プロンプト、指定参照、操作・状態・作成日時も確認できます。Plan hashは生成計画IDとは別の値です。

非対話実行・リダイレクト・0件ではエディタとキャッシュを作りません。codeコマンドが見つからない場合や起動要求が失敗した場合は、保存したキャッシュを保持して全文を端末に表示します。キャッシュの編集は案件に反映しません。

Windowsの保存先は `%LOCALAPPDATA%/ImageWorkbench/cache/lists/<キャッシュID>/listing.txt` です。LOCALAPPDATAがない場合と他OSは `~/.cache/image-workbench/lists/` を使います。各実行で別IDを発行し、取得日時と一覧を保持します。不要になったら次のコマンドで対象を確認して削除します。

```powershell
iwb cache clear                  # 一覧キャッシュ全件
iwb cache clear <cache-id>       # 指定した一覧キャッシュだけ
```

表示中の一覧も削除対象にできますが、VS Codeを終了させることはありません。確認後に新しく作られたキャッシュは削除しません。未知・破損した記録、リンク、想定外のファイルは保持します。案件データ・exports・エディタ自身の履歴は対象外です。

変更点のみを記載した[Notion仕様書 ver4.2](https://app.notion.com/p/3fcf252ab68381abb8eed67a27a47e71)とver4.1を併せて適用します。案件データの形式変更・移行は不要です。

## 実装状況

ver3向けのローカル管理基盤が存在しますが、Codex会話連携とver4の受け入れ条件への適合は作業中です。未実装の機能を完成済みとは扱いません。実際の画像生成を試す場合は、対象生成計画、入力画像、利用枠の状態を会話で確認し、個別に承認します。
