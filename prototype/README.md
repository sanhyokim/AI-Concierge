# 音声試作（初回）

[評価台本 v1.1](../docs/eval-scenarios-v1.md) に沿って、次の4候補を同じ台本で比べるための試作コードです。結果の報告は [試作報告 v1](../docs/prototype-report-v1.md) にあります。

| 候補 | 案 | 音声 |
| --- | --- | --- |
| A-marin | A：OpenAI Realtime | `marin` |
| A-cedar | A：OpenAI Realtime | `cedar` |
| B-kazuha | B：Polly（本番はConversationRelay経由） | `Kazuha-Neural` |
| B-takumi | B：Polly（本番はConversationRelay経由） | `Takumi-Neural` |

本番の番号の設定変更や、新しい有料契約は行いません。業者を使うスクリプトは、認証情報がなければ、外部へ何も送らずに終了します。

## 1. 業者を使わない実行（認証情報は不要）

必要なのは Python 3.11 以上だけです（標準ライブラリのみで動きます）。

```bash
python3 -m unittest discover -s prototype/tests -t .   # 単体テスト
python3 -m prototype.run_offline                       # シナリオ検査と計測ツールの精度検証
```

`run_offline` は `prototype/results/offline-<UTC時刻>.json` と `.md` を書き出します。内容は次の3つです。

- 決まった処理のシナリオ検査（R-03、I-02、X-01〜X-06）
- 時間が分かっている合成信号での、計測ツールの精度検証（声の評価ではない）
- S台本のSSMLの検査

## 2. 業者を使う実行（認証情報を用意したら）

### 準備

1. 環境の設定（セッションのタイトルバーにあるクラウド環境のメニュー → Edit）に、次の変数を登録します。新しいセッションから読み込まれます。認証情報はチャットに貼らないでください。

   | 変数 | 用途 |
   | --- | --- |
   | `OPENAI_API_KEY` | 案A |
   | `CONCIERGE_POLLY_ACCESS_KEY_ID`、`CONCIERGE_POLLY_SECRET_ACCESS_KEY`、`CONCIERGE_POLLY_REGION`（例：`ap-northeast-1`） | 案B（Polly） |

   Pollyには専用の変数名を使い、環境に既にある `AWS_*` の変数は使いません。

2. 必要なパッケージを入れます。

   ```bash
   pip install -r prototype/requirements-vendor.txt
   ```

3. 回数・時間・費用の上限と、利用量の台帳（`prototype/results/usage-ledger.jsonl`）は、どのスクリプトでも自動で効きます。上限を超える要求は送らずに止まります。件数・予算・止める条件は[最小の実測計画](../docs/measurement-plan-v1.md)にあります。

### 部分減速：同じ文章を通常版（N）と減速版（D）で読ませて比べる

```bash
python3 -m prototype.engines.realtime_probe   # 案A：marin・cedar。方法 split と instr
python3 -m prototype.engines.polly_probe      # 案B：Kazuha・Takumi。Polly直結（Relay経由ではない）
```

**案A（OpenAI Realtime）**
- `split`：pre・target・postの3つの応答に分け、targetのときだけ `audio.output.speed` を変えます。区間の境界は応答の切れ目なので、自動で測れます。
- `instr`：指示文で【】の中だけを遅く読ませます。区間の境界は自動では分かりません。書き出された `*.labels.json` に開始と終了の秒数を手で入れてから、次のコマンドで測ります。

  ```bash
  python3 -m prototype.measure.compare_labels <N>.labels.json <D>.labels.json
  ```

**案B（Polly）**
- SSMLの `<mark>` によるスピーチマークで区間を分け、`<prosody rate>`（初期値80%）で減速します。

**出力**
- どちらも、μ-lawを通したWAV（8kHz）と `report.json` を書き出します。`report.json` には、N版とD版の比較が入ります。
  - 発音速度比
  - 間の増え方
  - 通常の速さへの戻り
- 合否は、この数値と聴取評価をあわせて判断します（評価台本v1.1のS区分）。

### 対話（案A）

```bash
python3 -m prototype.engines.run_dialog --scenario R-03 --voice marin \
    --caller-audio-dir prototype/scenarios/caller_audio
```

**発信者の音声の用意**
- 発信者の各ターンの音声を、`first_round.json` の `audio` 欄にある名前のWAVで用意します。
- 人が台本を読んで録音するか、候補ではない音声合成で作ります。候補ではないPolly `Mizuki`（Standard）で作るときは次を実行します（作ったファイルは再利用され、2回目からは何も送りません）。

  ```bash
  python3 -m prototype.engines.make_caller_audio
  ```

**この実行で取れるもの**
- AIの音声（ターンごとのWAV）
- イベントの時刻
- 割り込みで止まるまでの時間（`barge_in`）
- 応答の遅延（`ai_audio_start`）
- 関数呼び出し
- 保存された項目と、その確認状態

**測定の地点**
- このクライアントのAPI側で測ります。電話網の遅延は含みません。

### 案B（ConversationRelay）

`engines/relay_app.py` は、WebSocketの処理のひな形です。実行には次の2つが必要で、どちらも今回の範囲外のため、まだ実行していません。

- Twilioのアカウントと番号（契約が要る）
- 公開された `wss://` のURL

TwiMLの生成、トークンの組み立て、割り込み時の履歴の切り詰め、AIを拒否されたときの `end` による受け渡しは、オフラインで検査しています。

## 3. ファイル構成

| ファイル | 内容 |
| --- | --- |
| `concierge/readings.py` | 電話番号・日付・時刻をカナにする、モーラを数える、「明日」などの相対的な日付を解決する（曖昧なときは確認が必要と返す） |
| `concierge/confirmation.py` | 項目の確認状態。直前に復唱した値に、明確に肯定したときだけ確認済みにする |
| `concierge/call_flow.py` | 同意と拒否の状態、「人と話したい」の分岐、プッシュボタンでの受付、障害時の扱い |
| `concierge/ssml.py` | 部分減速用のSSML（1つのトークンの中で閉じる。`<mark>` 付き） |
| `concierge/prompts.py` | 案Aの指示文と、関数の定義 |
| `measure/` | WAV、μ-law、発話区間の検出、発音速度と間の計測、合成信号での検証 |
| `engines/realtime_runner.py` | 案Aの対話試験を回す仕組み（通信部分は差し替えられる。テストでは偽のサーバーを使う） |
| `engines/realtime_probe.py`、`engines/polly_probe.py`、`engines/run_dialog.py` | 業者を使う計測（認証情報が必要） |
| `engines/relay_app.py` | 案Bのひな形（Twilioが必要） |
| `engines/budget.py` | 利用量の台帳、回数・音声の秒数・費用の上限、再試行の制限 |
| `engines/make_caller_audio.py` | 対話試験の発信者の音声を、候補ではない声で作る |
| `cost/` | 単価（`prices.json`）、費用比較表を作る `cost_model.py`、実測計画の費用の見込み `measurement_budget.py` |
| `realvoice/` | 公開音声での計測方法の予備確認（`fetch_clips.py`、`compare.py`、AIの仮ラベル） |
| `scenarios/first_round.json` | 初回の台本（架空のデータ） |
