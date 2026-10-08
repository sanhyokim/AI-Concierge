# 候補一覧 v1（公式資料で確認したこと・未確認・除外の理由）

- 確認日：2026-10-05。公式公開資料だけで確認した内容。アカウントごとの利用可否・実際の品質・実回線での性能は未確認。『日本語の記載あり』は品質の保証ではない。price.status: confirmed＝一次資料で確認 / reference＝『から』表記などプランで変わる / unconfirmed＝未確認（記号で残す）
- 業者APIの実行・試聴・通話による実測は行っていない。性能の順位や採用の決定ではない。
- 費用と品質は [候補の比較枠](./candidate-comparison-v1.md) で、同じ前提にそろえて比べる。

## 1. 一覧

| 候補 | 種類 | 日本語 | ブラウザーからの接続 | 料金（公式） | 位置づけ |
| --- | --- | --- | --- | --- | --- |
| OpenAI GPT-Live 1 | 会話モデル（同時に聞いて話す） | 公式の言語の一覧がなく、追加の声の表は英語・ポルトガル語だけ。言語は指示文で指定する（最初の接続で日本語の受付を確かめる） | WebRTC。こちらのサーバーが鍵を使って POST /v1/live/sessions でSDPを交換する（ブラウザーに鍵を渡さない） | $0.05/分 | 初回（試験の対象） |
| Google Gemini 3.8 Live | 会話モデル（音声） | Live APIの対応言語の表に日本語（ja）がある | 一時トークン（POST v1beta/auth_tokens、既定30分・1回）で、BidiGenerateContentConstrained のWebSocketにつなぐ | 音声入力 $0.005/分・出力 $0.018/分（目安） | 初回（試験の対象） |
| ElevenLabs ElevenAgents（Expressive mode） | 会話サービス | Expressive modeの資料が日本語を明記 | @elevenlabs/client。署名付きURL（15分）またはWebRTCのトークンをサーバーで発行 | $0.08/分（から） | 保留（費用が高い。性能に大差がなければ使わない） |
| Cartesia Managed Agents（Ink-2＋LLM＋Sonic-3.6） | 会話サービス（部品統合） | Ink-2・Sonic-3.6のモデル資料に日本語（ja） | 一時トークン（POST /access-token）で wss://api.cartesia.ai/agents/stream/{agent_id}?access_token= につなぐ | $0.06/分 | 保留（費用が高い。性能に大差がなければ使わない） |
| OpenAI GPT-Realtime-2.1（比較の基準） | 会話モデル（音声） | 日本語の試験は既存の計画で行う。自然さは未実測 | WebRTC（一時キー） | 旧費用表と同じトークン単価 | 基準（保留） |
| OpenAI GPT-Realtime-2.1-mini（比較の基準） | 会話モデル（音声） | 未実測 | WebRTC（一時キー） | 旧費用表と同じトークン単価 | 基準（保留） |
| Inworld Realtime API（STT-1＋LLM＋TTS-2） | 会話API（部品統合） | STT-1とTTS-2に日本語の記載 | WebSocketにBearerヘッダーが要る。ブラウザーからは中継が要る見込み（WebRTCはEarly access） | STT $0.15/時間、TTS $25/100万文字、LLM別 | 接続調査を優先（2回目） |
| Deepgram Voice Agent API（Flux multilingual＋LLM＋TTS） | 会話API | Fluxに日本語。管理型TTSの日本語の声は未確認 | 一時JWT（30秒）で /agent のWebSocket | $0.075/分 | 2回目以降 |
| Retell AI | 会話サービス | 対応表に日本語 | Web call SDK | $0.078/分 | 2回目以降 |
| Vapi | 会話サービス | AssemblyAIの日本語。TTSは別に選ぶ | Web SDK（公開鍵） | $0.05/分 | 2回目以降 |
| xAI grok-voice-think-fast-2.0 | 会話モデル（音声） | 対応表に日本語 | 一時トークン（xai-client-secret.） | $0.08/分 | 2回目以降 |
| Alibaba Qwen3.8-Omni-Flash-Realtime | 会話モデル（同時に聞いて話す） | 声の一覧に日本語 | Bearerの鍵だけで、一時トークンがない → 中継が要る | 単価は確認、換算は未確認 | 次の候補（GPT-Live・Geminiがよくなかった場合） |

## 2. 候補ごとの詳細

### OpenAI GPT-Live 1（初回（試験の対象））

- モデル・構成：gpt-live-1
- 公式で確認：2026-09-10に一般提供（変更履歴）／同時に聞いて話す設計。業務処理は delegation（responses／client）で別のモデル・処理へ渡す／エンドポイントは /v1/live/sessions。Realtimeとは別のプロトコル（移行ガイドあり）／$0.05/分、秒単位の課金。バックエンドのモデルとツールは別料金
- 日本語：公式の言語の一覧がなく、追加の声の表は英語・ポルトガル語だけ。言語は指示文で指定する（最初の接続で日本語の受付を確かめる）
- 接続：ブラウザーはWebRTC。こちらのサーバーが鍵を使って POST /v1/live/sessions でSDPを交換する（ブラウザーに鍵を渡さない）。電話はSIP、またはサーバーのWebSocketで中継
- 未確認：日本語の会話品質／発話の開始・終了と割り込みのイベント／バックエンドのモデルの費用（記号 L_live）
- 自社で作るもの：自社の中継・業務処理が要る（delegationの受け口）
- 位置づけの理由：同時に聞いて話す会話モデルの代表。相づち・訂正への対応を最初に確かめる
- 出典：[1](https://developers.openai.com/api/docs/models/gpt-live-1)、[2](https://developers.openai.com/api/docs/guides/voice-webrtc?api=live)、[3](https://developers.openai.com/api/docs/guides/live-delegation)、[4](https://developers.openai.com/api/docs/guides/live-migration)、[5](https://developers.openai.com/api/docs/changelog)

### Google Gemini 3.8 Live（初回（試験の対象））

- モデル・構成：gemini-3.8-live
- 公式で確認：Stable（2026年9月更新）／Live APIに割り込みの通知（serverContent.interrupted）と、発話検出の設定がある／3.8は非同期の関数実行が既定／Extended Thinking版（gemini-3.8-live-extended-thinking）もある／料金（1Mトークン）：音声入力$3（目安$0.005/分）、音声出力$12（目安$0.018/分）、テキスト入力$0.75・出力$4.50
- 日本語：Live APIの対応言語の表に日本語（ja）がある
- 接続：ブラウザーは一時トークン（POST v1beta/auth_tokens、既定30分・1回）で、BidiGenerateContentConstrained のWebSocketにつなぐ。電話はサーバーのWebSocketで中継
- 未確認：会話の履歴がターンごとに課金されるか（記号 Ctx_gem）／無料枠でのデータの扱い（試験は有料枠を前提）
- 自社で作るもの：自社の中継・業務処理が要る
- 位置づけの理由：日本語の明記があり、トークン単価が安い会話モデル。安い構成の代表として比べる
- 出典：[1](https://ai.google.dev/gemini-api/docs/models/gemini-3.8-live)、[2](https://ai.google.dev/gemini-api/docs/live-api/capabilities)、[3](https://ai.google.dev/gemini-api/docs/ephemeral-tokens)、[4](https://ai.google.dev/gemini-api/docs/pricing)、[5](https://ai.google.dev/gemini-api/docs/models/gemini-3.8-live-extended-thinking)

### ElevenLabs ElevenAgents（Expressive mode）（保留（費用が高い。性能に大差がなければ使わない））

- モデル・構成：音声認識＋選んだLLM＋Eleven v3 Conversational＋独自の発話区切り
- 公式で確認：音声認識・選んだLLM・音声合成・独自の発話区切りのモデルを統合／割り込みの設定、ツール実行中の割り込みがある／Expressive modeは声の調子も発話区切りに使う／課金は接続時間。10秒を超える無音は95%引き。LLMは別料金／「$0.08/分から」（プランで変わる）／Eleven v4／v4 Turboは日本語に対応するが、Agentsで選べるかは別
- 日本語：Expressive modeの資料が日本語を明記
- 接続：ブラウザーは@elevenlabs/client。署名付きURL（15分）またはWebRTCのトークンをサーバーで発行。電話はTwilio連携・SIP。register call方式なら分岐を自社に置ける
- 未確認：プランごとの実際の分単価／LLMの費用（記号 L_11）／プランの月額（記号 P_11）
- 自社で作るもの：業者側でエージェントを設定。自社は業務処理（ツール）と記録
- 位置づけの理由：独自の発話区切りのモデルと、会話向けの日本語の声を持つ会話サービスの代表
- 出典：[1](https://elevenlabs.io/docs/eleven-agents/overview)、[2](https://elevenlabs.io/docs/eleven-agents/customization/voice/expressive-mode)、[3](https://elevenlabs.io/docs/eleven-agents/customization/conversation-flow)、[4](https://elevenlabs.io/docs/eleven-agents/customization/authentication)、[5](https://elevenlabs.io/docs/help-center/product/eleven-agents/how-much-does-eleven-agents-cost)、[6](https://elevenlabs.io/docs/overview/models)

### Cartesia Managed Agents（Ink-2＋LLM＋Sonic-3.6）（保留（費用が高い。性能に大差がなければ使わない））

- モデル・構成：ink-2 ／ sonic-3.6-2026-08-27
- 公式で確認：Ink-2（Stable、2026-09-17）とSonic-3.6（Stable、sonic-3.6-2026-08-27）が日本語に対応／$0.06/分（有料プラン）／月額：Free $0、Pro $5、Startup $49、Scale $299／Cartesiaが用意する番号は米国のみ（$0.014/分の追加）。Twilio・SIPの番号も使える
- 日本語：Ink-2・Sonic-3.6のモデル資料に日本語（ja）
- 接続：ブラウザーは一時トークン（POST /access-token）で wss://api.cartesia.ai/agents/stream/{agent_id}?access_token= につなぐ。電話はTwilio・SIPの番号（米国番号の料金は日本に流用しない）
- 未確認：LLMの料金（料金ページは『UIで作ったエージェントは期間限定で無料』、製品ページは『トークン単価で課金』と食い違う。記号 L_cart）／ブラウザーで業務処理（ツール）を受けられるか
- 自社で作るもの：業者側でエージェントを設定
- 位置づけの理由：費用面で有力な部品統合型。日本語のSTT・TTSを公式資料で確認できた
- 出典：[1](https://www.cartesia.ai/pricing)、[2](https://www.cartesia.ai/product/ai-phone-agent)、[3](https://docs.cartesia.ai/build-with-cartesia/stt/latest)、[4](https://docs.cartesia.ai/build-with-cartesia/tts-models/latest)、[5](https://docs.cartesia.ai/api-reference/agents/websocket)、[6](https://docs.cartesia.ai/get-started/authenticate-your-client-applications)

### OpenAI GPT-Realtime-2.1（比較の基準）（基準（保留））

- モデル・構成：gpt-realtime-2.1
- 公式で確認：既存の試作の基準。料金は旧費用表と同じ／出力の上限 max_output_tokens（1〜4096）／ブラウザーは一時キー（/v1/realtime/client_secrets）とWebRTC（/v1/realtime/calls）
- 日本語：日本語の試験は既存の計画で行う。自然さは未実測
- 接続：ブラウザーはWebRTC（一時キー）。電話はMedia Streams・SIP
- 未確認：キャッシュが実際に成立する率
- 自社で作るもの：自社の中継・業務処理が要る（既存の試作がある）
- 位置づけの理由：比較の基準。GPT-Liveと同じ構成とはみなさない
- 出典：[1](https://developers.openai.com/api/docs/guides/voice-webrtc?api=realtime)、[2](https://developers.openai.com/api/reference/resources/realtime/client-events)、[3](https://developers.openai.com/api/docs/pricing)

### OpenAI GPT-Realtime-2.1-mini（比較の基準）（基準（保留））

- モデル・構成：gpt-realtime-2.1-mini
- 公式で確認：料金は旧費用表と同じ
- 日本語：未実測
- 接続：ブラウザーはWebRTC（一時キー）。電話はMedia Streams・SIP
- 未確認：日本語の会話品質
- 自社で作るもの：同上
- 位置づけの理由：安い側の基準
- 出典：[1](https://developers.openai.com/api/docs/pricing)

### Inworld Realtime API（STT-1＋LLM＋TTS-2）（接続調査を優先（2回目））

- モデル・構成：inworld-stt-1 ／ TTS-2・TTS-2 Flash
- 公式で確認：OpenAI Realtimeのプロトコルに沿う／Research preview。WebSocketはGA、WebRTC・SIPはEarly access／STT-1 $0.15/時間、TTS-2 $25/100万文字、Flash $15/100万文字、LLMは原価
- 日本語：STT-1とTTS-2に日本語の記載
- 接続：ブラウザーはWebSocketにBearerヘッダーが要る。ブラウザーからは中継が要る見込み（WebRTCはEarly access）。電話はSIPはEarly access
- 未確認：セッショントークンを発行する方法／LLMの費用（記号 L_inw）／STTの課金が通話全体の時間か、話した時間か
- 自社で作るもの：自社の中継が要る（ブラウザー・電話とも）
- 位置づけの理由：費用面で有力。ただし提供段階がResearch previewで、ブラウザーからの接続に中継が要るため、初回の会話試験は2回目に回し、接続の調査を先に行う
- 出典：[1](https://docs.inworld.ai/realtime/overview)、[2](https://inworld.ai/speech-to-speech)、[3](https://docs.inworld.ai/realtime/connect/websocket)、[4](https://docs.inworld.ai/stt/languages)、[5](https://inworld.ai/tts)、[6](https://inworld.ai/pricing)

### Deepgram Voice Agent API（Flux multilingual＋LLM＋TTS）（2回目以降）

- モデル・構成：flux-general-multi
- 公式で確認：Standard $0.075/分（接続時間）。BYO LLM $0.065、BYO LLM＋TTS $0.050／Flux multilingualに日本語（ja）／管理型のCartesia TTSはStandardに含まれる
- 日本語：Fluxに日本語。管理型TTSの日本語の声は未確認
- 接続：ブラウザーは一時JWT（30秒）で /agent のWebSocket。電話は中継
- 未確認：Standardに含まれるLLMの範囲／日本語の声
- 自社で作るもの：自社の中継が要る
- 位置づけの理由：料金に含まれる範囲が広い会話API。日本語の声の確認後に比べる
- 出典：[1](https://deepgram.com/pricing)、[2](https://developers.deepgram.com/docs/models-languages-overview/)、[3](https://developers.deepgram.com/docs/voice-agent-tts-models)

### Retell AI（2回目以降）

- モデル・構成：LLM・STT・TTSを選ぶ
- 公式で確認：基盤 $0.055/分、声 $0.015/分（ElevenLabsは$0.040）、LLMは別（例：GPT 5 mini $0.008/分）／日本語（ja-JP）に対応するSTT・TTSの一覧あり
- 日本語：対応表に日本語
- 接続：ブラウザーはWeb call SDK。電話はTwilio・SIP
- 未確認：電話・追加機能の料金／選ぶLLMでの品質
- 自社で作るもの：業者側で設定
- 位置づけの理由：安い部品の組み合わせを試せる会話サービス
- 出典：[1](https://www.retellai.com/pricing)、[2](https://docs.retellai.com/build/language-support)、[3](https://docs.retellai.com/deploy/web-call)

### Vapi（2回目以降）

- モデル・構成：STT・LLM・TTSを選ぶ
- 公式で確認：基盤 $0.05/分＋各モデルの原価／発話の終わり・割り込みの設定（既定は音声検出）／AssemblyAI Universal 3.5/3.6 Proに日本語
- 日本語：AssemblyAIの日本語。TTSは別に選ぶ
- 接続：ブラウザーはWeb SDK（公開鍵）。電話はTwilio・SIP
- 未確認：STT・LLM・TTSの費用（記号 V_vapi）
- 自社で作るもの：業者側で設定
- 位置づけの理由：構成を細かく調整できる会話サービス
- 出典：[1](https://vapi.ai/pricing)、[2](https://docs.vapi.ai/customization/voice-pipeline-configuration)、[3](https://docs.vapi.ai/providers/transcriber/assembly-ai)

### xAI grok-voice-think-fast-2.0（2回目以降）

- モデル・構成：grok-voice-think-fast-2.0（別名 grok-voice-latest は使わず、名前を固定する）
- 公式で確認：日本語（ja）／server_vadによる割り込み／$0.08/分。テキスト入力に$0.004の表記
- 日本語：対応表に日本語
- 接続：ブラウザーは一時トークン（xai-client-secret.）。電話は電話向けの符号化・SIPの案内
- 未確認：一時トークンを発行するエンドポイント／テキスト入力の課金単位（記号 T_grok）／相づちの扱い
- 自社で作るもの：自社の中継・業務処理が要る
- 位置づけの理由：割り込みの挙動を比べる候補
- 出典：[1](https://docs.x.ai/developers/model-capabilities/audio/speech-to-speech)、[2](https://docs.x.ai/developers/pricing)

### Alibaba Qwen3.8-Omni-Flash-Realtime（次の候補（GPT-Live・Geminiがよくなかった場合））

- モデル・構成：qwen3.8-omni-flash-realtime
- 公式で確認：full-duplex、semantic_vad（相づちや雑音による誤ったターン検出を減らす）／音声の履歴は100ターン・600秒まで（確認済みの値はアプリ側にも保持する）／料金はシンガポールと北京だけ。シンガポール：1Mトークンあたり音声入力$0.93・音声出力$1.87
- 日本語：声の一覧に日本語
- 接続：ブラウザーはBearerの鍵だけで、一時トークンがない → 中継が要る。電話は中継
- 未確認：音声のトークン換算（記号 Q_tok）／処理の地域（シンガポール・北京）とデータの扱い／シンガポールでも、データの保存の地域と推論の場所が同じかが明記されていない／学習への利用の記載が資料で異なる／ブラウザー向けの短命の接続情報がなく、中継のサーバーが要る／運営は中国の企業グループ（判断の材料として記録）
- 自社で作るもの：自社の中継が要る
- 位置づけの理由：仕様は優先条件に合うが、処理の地域とデータの扱いの判断が先
- 出典：[1](https://www.alibabacloud.com/help/en/model-studio/realtime)、[2](https://www.alibabacloud.com/help/en/model-studio/omni-voice-list)、[3](https://www.alibabacloud.com/help/en/model-studio/model-pricing)

## 3. 除外・保留・部品として扱うもの

比較資料（2026-10-05）の判断を引き継いだもの。今回、この表の出典は再確認していない。

| 区分 | 対象 | 理由 |
| --- | --- | --- |
| 除外 | Amazon Nova 2 Sonic | 公式の対応言語に日本語がない（[出典](https://docs.aws.amazon.com/nova/latest/nova2-userguide/sonic-language-support.html)） |
| 除外 | NVIDIA PersonaPlex 7B v1 | full-duplexの研究上の参考。公式モデルカードの用途は英語（[出典](https://huggingface.co/nvidia/personaplex-7b-v1)） |
| 除外 | Hume EVI | EVIとTTS APIが2026年11月13日（米国東部時間）に終了すると公式に告知（[出典](https://dev.hume.ai/intro)） |
| 保留 | Bland | 日本語の入力・出力の構成を確認しきれていない |
| 保留 | Ultravox | 公式サイトがInworldへの加入を案内。独立した候補として二重に数えない |
| 保留 | Doubao／Seeduplex | モデル一覧の本文を取得できず、日本語のAPI対応と料金が未確認 |
| 部品として比較 | Amazon Polly単体などの音声合成 | 声の比較用。会話の理解や同時発話への対応は判定できない |
| 部品として比較 | ConversationRelay・LiveKit・Pipecatなど | 接続・会話構成の選択肢。組み合わせた全体の挙動と費用で評価する |
| 部品として比較 | Fish Audio・MiniMax・Soniox・AssemblyAI・Azure Speechなど | 音声合成・認識の部品。会話サービス全体と同一視しない |

## 4. 初回の会話試験の構成（利用者の決定。採用の決定ではない）

| 構成 | 位置づけ | 比べる意味 |
| --- | --- | --- |
| OpenAI GPT-Live 1 | 初回（試験の対象） | 同時に聞いて話す会話モデルの代表。相づち・訂正への対応を最初に確かめる |
| Google Gemini 3.8 Live | 初回（試験の対象） | 日本語の明記があり、トークン単価が安い会話モデル。安い構成の代表として比べる |
| ElevenLabs ElevenAgents（Expressive mode） | 保留（費用が高い。性能に大差がなければ使わない） | 独自の発話区切りのモデルと、会話向けの日本語の声を持つ会話サービスの代表 |
| Cartesia Managed Agents（Ink-2＋LLM＋Sonic-3.6） | 保留（費用が高い。性能に大差がなければ使わない） | 費用面で有力な部品統合型。日本語のSTT・TTSを公式資料で確認できた |
| OpenAI GPT-Realtime-2.1（比較の基準） | 基準（保留） | 比較の基準。GPT-Liveと同じ構成とはみなさない |
| OpenAI GPT-Realtime-2.1-mini（比較の基準） | 基準（保留） | 安い側の基準 |
| Alibaba Qwen3.8-Omni-Flash-Realtime | 次の候補（GPT-Live・Geminiがよくなかった場合） | 仕様は優先条件に合うが、処理の地域とデータの扱いの判断が先 |

- 最初の接続の確認は GPT-Live 1 と Gemini 3.8 Live の2つだけで行う（2026-10-07）。保留の候補は、採用の候補からは外していない。
- GPT-Liveは、日本語の受付を最初に確かめる。業務処理を受け持つ裏方のモデル（delegation）も確かめる。
- Inworldは、接続の調査を優先して進める（ブラウザーからは中継が要り、提供段階がResearch previewのため、会話試験は2回目）。
- Grok・Deepgram・Retell・Vapiは、初回の結果と未確認の点の確認の後に比べる。
- 「同時に聞いて話す」「低遅延」などの機能名だけで順位を付けない。採用は共通の会話試験で決める。
