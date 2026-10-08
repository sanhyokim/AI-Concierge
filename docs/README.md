# 文書の一覧

**仕様の正本は [spec-draft-v1.md](./spec-draft-v1.md)（仕様書ドラフト v1）です。** ほかの文書は、検討の経緯と根拠です。内容が食い違ったときは、正本を優先します。

## 正本

| 文書 | 内容 |
| --- | --- |
| [spec-draft-v1.md](./spec-draft-v1.md) | 確定した利用者要件、機能仕様の要点、未確定事項、一括確認のお願い（FAQ・案内文・合格基準の推奨案） |

## 正本を支える資料（今回の版）

| 文書 | 内容 |
| --- | --- |
| [start-mac.md](./start-mac.md) | **はじめての起動（Mac）**：Pythonの入れ方から、`start_mac.command` での起動、うまくいかないときまで |
| [start-windows.md](./start-windows.md) | **はじめての起動（Windows）**：Pythonの入れ方から、ダブルクリックでの起動、APIキーの入れ方、うまくいかないときまで |
| [admin-app-v1.md](./admin-app-v1.md) | 管理画面と受付の共通処理（動かし方・画面・判定の規則・データの扱い・試験の結果。画面の画像は `screenshots/`） |
| [candidates-v1.md](./candidates-v1.md) | 候補一覧（公式資料で確認したこと・未確認・除外の理由・初回の構成〔GPT-Live 1・Gemini 3.8 Live。ほかは保留〕） |
| [candidate-comparison-v1.md](./candidate-comparison-v1.md) | 候補の比較枠（同じ前提での月額・初期費用・品質の欄・受付1件あたりの費用。8章は採用した回線案2の月額を、今の携帯への転送を基準に計算。1〜7章の回線案1は採用しない参考） |
| [browser-lab-setup.md](./browser-lab-setup.md) | ブラウザー会話試験の準備（最初の会話の手順・開始の回数・終わり方・設定の反映・発話での拒否・接続の確認状況） |
| [measurement-plan-v2.md](./measurement-plan-v2.md) | 実測計画 v2.2（接続の予備試験 → 詳しい会話比較。予算は支払い額・既知の費目の試算・未確認の費用・アプリ内の制限に分けて示す） |
| [eval-scenarios-v1.md](./eval-scenarios-v1.md) | 評価台本 v1.1 |
| [voice-candidates-v1.md](./voice-candidates-v1.md) | 候補音声の公式対応表 |

## 旧構成の資料（参考）

回線案1（常にクラウドへ転送してアプリが振り分ける）と、Twilio ConversationRelay・Polly などの旧構成で作った資料です。今の構成（回線案2・音声から音声へ直接の候補）の判断には使いません。旧構成のコード（`prototype/engines/polly_probe.py`・`relay_app.py`・`realtime_runner.py` など）は、仕様の「旧案Bを比較の基準に残す」に合わせて残しています。

| 文書 | 内容 |
| --- | --- |
| [cost-comparison-v1.md](./cost-comparison-v1.md) | 費用比較表（旧構成の案A・案B）。`python3 -m prototype.cost.cost_model` で作り直せる |
| [measurement-plan-v1.md](./measurement-plan-v1.md) | 実測計画 v1（API直接・Polly単体の詳しい測定。順序と予算はv2で置き換え） |
| [realvoice-method-check-v1.md](./realvoice-method-check-v1.md) | 公開音声での計測方法の予備確認（AIの仮ラベル。候補の採用判断には使わない） |
| [prototype-report-v1.md](./prototype-report-v1.md) | 音声試作の報告 v1 |

## 経緯

| 文書 | 内容 |
| --- | --- |
| [design-prompt-v1.md](./design-prompt-v1.md) | 設計議論用プロンプト（元の要件と合意表） |
| [design-review-v1.md](./design-review-v1.md) | 設計レビュー v1 |
| [design-request-v2.md](./design-request-v2.md) | 改訂依頼 v2 |
| [design-review-v2.md](./design-review-v2.md) | 設計レビュー v2.1（詳細の根拠として参照） |
| [design-feedback-v2.md](./design-feedback-v2.md) | v2への返答 |
