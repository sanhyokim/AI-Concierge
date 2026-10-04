# 候補音声の公式対応表 v1

- 確認日：2026年10月4日。公式資料だけを使い、各行に出典を付けています。
- 関連文書：[設計レビュー v2.1](./design-review-v2.md)の3-1・3-4、[評価台本](./eval-scenarios-v1.md)のS区分・V区分
- 目的：音声の試験に使う候補を絞ることです。
- 決まらないこと：日本語の自然さ、部分減速が実際に聞き取りやすいか、電話での品質は、この表では決まりません【要実測】。

## 確認のルール

候補ごとに、次の4つを**一組として**確認しました。

1. 音声ID（実際に指定する値）
2. エンジン（またはモデル）
3. ConversationRelay（以下、Relay）で使えるか
4. 部分減速に使えるSSMLに対応しているか

確認の方法と注意点は次のとおりです。

**音声の一覧の根拠**
- Polly本体やGoogle本体で使えることだけを根拠に、Relayでも使えるとは扱いません。
- RelayでのGoogleとAmazonの音声は、Relayの公式資料が参照先に指定している「Twilio TTS Voices」の一覧で確認しました。
  - Relayの資料には「For voices from Google or Amazon (including generative options), refer to our Twilio TTS Voices documentation」と書かれています【公式】（[T1]）。
  - 一覧のページには、`<ConversationRelay>`用の書式として`{Voice}`（例：`Joanna-Generative`）が載っています。`<Say>`用の`{Provider}.{Voice}`とは書式が違います【公式】（[T2]）。

**SSMLの扱い**
- Relayは、テキストトークンの中のSSMLを、そのまま音声合成に渡します。
  - 公式の説明は「you can passthrough SSML tags within the `token` to … increase or decrease the speed of spoken text」です【公式】（[T4]）。
- SSMLのタグが複数のトークンにまたがった場合の動きは【未確認】です。部分減速のタグは、1つのトークンの中で閉じるように作ります（[T4]の例も1つのトークンに収まっています）。

**話速の指定**
- Relayには、GoogleやAmazonの話速を指定する属性が見当たりません（[T3]）。
- 話速を部分的に変える手段は、SSMLだけです。

## 1. 一覧

| No | 事業者（Relayの`ttsProvider`） | 音声ID（実際に指定する値） | エンジン・モデル | 性別（公式の表記） | Relayで使えるか | 部分減速の手段 | 声全体の話速 | 試験での扱い |
| --- | --- | --- | --- | --- | --- | --- | --- | --- |
| P1 | Amazon | `Kazuha-Neural` | Polly Neural | 女性 | 【公式】一覧に掲載（[T2]） | SSMLの`<prosody rate>`。Neuralはrateとvolumeに対応し、文全体を囲む制約はGenerativeだけにある【公式】（[P2]） | SSMLのrateで指定（20〜200%）【公式】 | **女性系の第一候補**（部分減速の公式根拠がそろっている） |
| P2 | Amazon | `Tomoko-Neural` | Polly Neural | 女性 | 【公式】（[T2]） | P1と同じ | P1と同じ | 女性系の候補 |
| P3 | Amazon | `Takumi-Neural` | Polly Neural | 男性 | 【公式】（[T2]） | P1と同じ | P1と同じ | **男性系の第一候補**（PollyのNeuralで唯一の日本語男性音声） |
| P4 | Amazon | `Takumi` | Polly Standard | 男性 | 【公式】（[T2]） | Standardはprosodyのすべての属性に対応【公式】（[P2]） | 同上 | 比較用（Standardのため品質は低めの可能性【要実測】） |
| P5 | Amazon | `Mizuki` | Polly Standard | 女性 | 【公式】（[T2]） | P4と同じ | 同上 | 比較用 |
| G1 | Google | `ja-JP-Wavenet-B` | WaveNet | 女性 | 【公式】（[T2]） | SSMLの`<prosody rate>`。ただしGoogleは「文全体を囲む形でだけ使う。文の途中の単語を囲むと不要な間が入ることがある」と注意している【公式】（[G2]）。番号や日時は独立した文にして囲む | SSMLで指定 | 候補（文単位での減速を試す） |
| G2 | Google | `ja-JP-Wavenet-C` / `ja-JP-Wavenet-D` | WaveNet | 男性 | 【公式】（[T2]） | G1と同じ | G1と同じ | 候補 |
| G3 | Google | `ja-JP-Standard-B`（女性）／`-C`・`-D`（男性） | Standard | 女性／男性 | 【公式】（[T2]） | G1と同じ | G1と同じ | 比較用 |
| G4 | Google | `ja-JP-Chirp3-HD-Aoede`／`-Kore`／`-Leda`／`-Zephyr` | Chirp 3 HD（Twilioの区分ではGenerative） | 女性 | 【公式】（[T2]） | **【未確認】** Chirp 3 HDのSSMLは同期の要求だけで、ストリーミングでは使えない【公式】（[G3]）。Relayがどちらの方式で要求するかは書かれていない | Google API単体ならspeaking_rateを指定できるが、Relayからの指定方法は【未確認】 | 声の質の比較用。部分減速は未確認 |
| G5 | Google | `ja-JP-Chirp3-HD-Charon`／`-Fenrir`／`-Orus`／`-Puck` | 同上 | 男性 | 【公式】（[T2]） | G4と同じ | G4と同じ | 同上 |
| G6 | Google | `ja-JP-Neural2-B`（女性）／`-C`・`-D`（男性） | Neural2 | 女性／男性（[G1]） | **【未確認】**（Twilioの一覧に載っていない） | — | — | **試験に使わない**（Relayで使えるか確認できるまで） |
| E1 | ElevenLabs | `3JDquces8E8bkmvbh6Bc`（Relayの日本語の既定音声） | 既定は`flash_v2_5`。`turbo_v2_5`も指定でき、どちらも日本語対応。`flash_v2`と`turbo_v2`は英語のみなので使わない【公式】（[T1]、[E1]） | 【未確認】 | 【公式】日本語の既定（[T1]） | **使えない前提**：ElevenLabsのSSMLは英語（en-US）の`<phoneme>`だけ【公式】（[T1]） | 音声IDの末尾で`speed`を0.7〜1.2で指定（声全体）【公式】（[T1]） | 声の質の比較用。部分減速の必須条件を満たすには、別の手段が必要 |
| O1 | OpenAI Realtime（Relayでは使えない。案A） | `marin` | `gpt-realtime-2.1` | 公式の表記なし【未確認】 | 対象外（Relayの`ttsProvider`はGoogle・Amazon・ElevenLabsのみ【公式】（[T3]）） | ①指示文でテンポを落とす（「指示で速く・遅く話させることもできる」【公式】（[O2]）。日本語の番号や日時だけを確実に遅くできるかは【要実測】） ②同じ声のまま応答を分け、その間で`audio.output.speed`を変える（ターンの間でだけ変更できる【公式】） | `audio.output.speed`は0.25〜1.5。生成した後の音声に対する調整【公式】（[O2]） | **案Aの第一候補**（公式の推奨音声） |
| O2 | OpenAI Realtime | `cedar` | 同上 | 【未確認】 | 対象外 | O1と同じ | O1と同じ | **案Aの第一候補**（公式の推奨音声） |
| O3 | OpenAI Realtime | `alloy`、`ash`、`ballad`、`coral`、`echo`、`sage`、`shimmer`、`verse` | 同上 | 【未確認】 | 対象外 | O1と同じ | O1と同じ | 試聴だけ |

**OpenAI Realtimeについて**
- 声は、セッションの中で一度音声を出した後は変えられません【公式】（[O1]）。通話ごとに選ぶことになります。
- 公式の対応言語の一覧と、SSMLへの対応は見つかりませんでした【未確認】。

## 2. 「明瞭な女性系」「穏やかな男性系」の仮の割り当て

公式の性別の表記だけで、仮に割り当てています。「はきはき」「穏やか」といった印象は、電話経路を通した試聴で決めます【要実測】。

| 系統 | 案B（Relay）の候補 | 案Aの候補 |
| --- | --- | --- |
| 明瞭な女性系 | P1 `Kazuha-Neural`、P2 `Tomoko-Neural`、G1 `ja-JP-Wavenet-B`、G4（Chirp 3 HDの女性音声） | O1・O2・O3から試聴で選ぶ（性別の公式表記なし） |
| 穏やかな男性系 | P3 `Takumi-Neural`、G2 `ja-JP-Wavenet-C`／`-D`、G5（Chirp 3 HDの男性音声） | 同上 |

## 3. 部分減速の実装メモ（Relay＋SSML。試験で調整する前提）

- 減速したい区間は、**1つのテキストトークンの中で開いて閉じます**。複数のトークンにまたがった場合の動きは【未確認】です。
- Polly Neural（P1〜P3）の例です。rateの値は仮で、試験で調整します。

  ```xml
  お電話番号を確認します。<prosody rate="80%">ゼロキュウゼロ、イチニーサンヨン、ゴーロクナナハチ</prosody>。こちらでお間違いないでしょうか。
  ```

- Google WaveNet（G1〜G3）の例です。番号を独立した文にして、文全体を囲みます。

  ```xml
  お電話番号を確認します。<prosody rate="slow">ゼロキュウゼロ、イチニーサンヨン、ゴーロクナナハチ。</prosody>こちらでお間違いないでしょうか。
  ```

- Relayが`<speak>`で自動的に囲むかどうかは【未確認】です。囲まなければならない場合の書き方も、試験の最初に確かめます。
- タグを解釈できない組み合わせでは、タグが読み上げられたり、エラーになったりするおそれがあります。最初の試験で、タグがそのまま音声にならないことを確認します。

## 4. この表で決まること、決まらないこと

**決まること**
- 部分減速を、SSMLで試せる候補：P1〜P5、G1〜G3
- Relayで使えるかを確認できていないので、試験に使わない候補：G6
- ElevenLabs（E1）では、日本語のSSMLによる部分減速を前提にしないこと

**決まらないこと**【要実測】
- 日本語の自然さ
- 部分減速が「通常→減速→通常」として自然に聞こえるか
- 電話の音質（8kHz）での明瞭さ
- 遅延
- 声の印象

## 5. 未確認のまま残っている項目

1. RelayがGoogleやPollyにストリーミングで要求するか、同期で要求するか。これによってChirp 3 HD（G4・G5）でSSMLが効くかが決まります。
2. SSMLのタグが複数のテキストトークンにまたがった場合の、Relayの動き。
3. `ja-JP-Neural2-*`、`ja-JP-Wavenet-A`、`ja-JP-Standard-A`、そしてTwilioの一覧に載っていないChirp 3 HDの音声名を、Relayで使えるか。
4. RelayでGoogleの話速（speaking_rate）を、声単位で指定する方法。
5. ElevenLabsの既定音声`3JDquces8E8bkmvbh6Bc`の性別と名前。
6. OpenAI Realtimeの日本語対応の明記と、SSMLへの対応。
7. Relayが`<speak>`で自動的に囲むかどうか。

## 出典（確認日：2026年10月4日）

**Twilio**
- [T1] [ConversationRelay — Picking a voice](https://www.twilio.com/docs/voice/conversationrelay/voice-configuration)：Google・Amazonの音声の参照先、ElevenLabsのモデルと話速、プロバイダーごとのSSML対応、日本語の既定構成
- [T2] [Twilio TTS Voices（Text-to-Speech）](https://www.twilio.com/docs/voice/twiml/say/text-speech)：音声の一覧と、`<ConversationRelay>`用の書式`{Voice}`
- [T3] [TwiML `<ConversationRelay>`](https://www.twilio.com/docs/voice/twiml/connect/conversationrelay)：`ttsProvider`の選択肢、無効な組み合わせでの切断
- [T4] [ConversationRelay WebSocket messages](https://www.twilio.com/docs/voice/conversationrelay/websocket-messages)：テキストトークンでのSSMLの受け渡し

**Amazon Polly**
- [P1] [Amazon Polly — Available voices](https://docs.aws.amazon.com/polly/latest/dg/available-voices.html)：日本語の音声とエンジン（Mizuki：Standard、Takumi：Neural／Standard、Kazuha・Tomoko：Neural。Generativeはなし）
- [P2] [Amazon Polly — prosody](https://docs.aws.amazon.com/polly/latest/dg/prosody-tag.html)：エンジンごとのrateへの対応。Generativeは文全体を囲む場合に限る
- [P3] [Amazon Polly — Supported SSML tags](https://docs.aws.amazon.com/polly/latest/dg/supportedtags.html)：Neuralでの`<break>`の対応

**Google**
- [G1] [Google Cloud TTS — Voices and types](https://docs.cloud.google.com/text-to-speech/docs/list-voices-and-types)
- [G2] [Google Cloud TTS — SSML](https://docs.cloud.google.com/text-to-speech/docs/ssml)：prosodyは文全体を囲む形で使う
- [G3] [Google Cloud TTS — Chirp 3 HD](https://docs.cloud.google.com/text-to-speech/docs/chirp3-hd)：SSMLはストリーミングでは使えない

**ElevenLabs**
- [E1] [ElevenLabs — Models](https://elevenlabs.io/docs/overview/models)：モデルごとの対応言語

**OpenAI**
- [O1] [OpenAI Realtime conversations](https://developers.openai.com/api/docs/guides/realtime-conversations)：音声の一覧、推奨音声、声を変えられない制約
- [O2] [OpenAI Realtime client events](https://developers.openai.com/api/reference/resources/realtime/client-events)、[Voice prompting](https://developers.openai.com/api/docs/guides/voice-prompting)：speedは後処理で、指示で速く・遅くできる
