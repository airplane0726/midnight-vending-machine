# 公開準備（2026-11-25 時点）

方針: 3日に1作品。月の上限 3,000円（失敗・作り直し・保存・投稿・サイト運営を含む）、1作品は原則 300円以内。
自動入金は禁止。当面は、完成作品を利用者が確認してから公開する。

## 1. 工程ごとの状態

| 工程 | 状態 | コマンド・置き場所 |
|---|---|---|
| 台本の作成・検証・履歴 | 自動化済み | `scaffold` → `validate` → `commit`（Claude Code が書く） |
| 画像生成（有料） | 自動化済み。費用の承認は利用者 | `images --allow-paid`（予算と公開の関門つき） |
| 動画の動き | 自動化済み（無料の合成・編集） | `roughcut`（edits/epNNN_roughcut.json）。動画生成AI は不採用 |
| 音（効果音・環境音・音楽） | 自動化済み（無料の合成） | `soundtrack.py`（roughcut の中で使う） |
| 投稿用の書き出し・自動確認 | 自動化済み | `finalize`（仕様・音量・音割れ・長さ・キーフレーム） |
| 作者ロゴの重ね・原版と投稿版の保存 | 自動化済み | `publish package`（final＝ロゴなし原版、post＝投稿版） |
| 表紙・静止画・各投稿先の文章 | 自動化済み（型から作る） | `publish package`（edits/epNNN_covers.json、edits/epNNN_publish.json、publish/templates/） |
| Google ドライブ保存・照合 | 自動化済み | `drive-token … --then persist + restore --check` |
| 承認と二重投稿の防止 | 自動化済み（記録と関門） | `publish request-review / approve / check / record`（publish/ledger.json） |
| 作品サイト | ローカル下書きのみ | `site`（site_draft/。公開しない） |
| 作者ブランド名・ロゴ | **未確定**（仮名で動く） | `brand/brand.json`（status: provisional） |
| SNS への投稿 | **未実装**（投稿先が未定） | 下記の調査を見て、投稿先が決まってから |
| 定期実行（3日ごと） | **未開始** | 投稿の流れが決まってから |

## 2. 利用者の操作が必要なこと

1. 作者ブランド名を決める（`brand/candidates.md`）。各サービスのハンドルの空き・同名の商標を確認する。
2. 投稿先を決め、アカウントを作る（または既存を使う）。取得した URL を `brand/brand.json` の `links` に書く。
3. 第1話の確認: `publish package` → `publish request-review` の後、投稿版の MP4・表紙・文章を見て、承認の意思を伝える（sha256 の先頭で、見たファイルを指定）。
4. 各サービスの投稿画面で投稿する（当面は手動）。投稿したら URL を伝える（`publish record` で記録）。
5. API で投稿したくなったら、下記の各サービスで開発者登録・認証を行う（費用・審査は下表）。
6. AI 利用の表示文を決める（series.json の `ai_disclosure` を、実際の制作方法に合わせて変えるか）。

## 3. 投稿 API の調査（公式資料の検索結果。2026-11-25）

この環境からは各公式サイトのページを直接開けなかったため、公式ドメインに絞った検索結果の要約で確認した。
**実際に接続する前に、もう一度公式ページで確認する。**

| サービス | 公式 API での投稿 | 必要な認証・審査 | 費用 | 自動投稿の制約 | 当面の扱い |
|---|---|---|---|---|---|
| YouTube | できる（videos.insert） | Google Cloud のプロジェクトと OAuth（youtube.upload など）。API キーだけでは不可 | API は無料。既定の割り当てで1日 100 回の投稿 | 未審査プロジェクトの投稿は「非公開」に制限されてきた。改訂履歴で 2026-11-02 にこの制限を終えたという記載が見つかったが、参照ページによって記述が食い違う。`privacyStatus=private` で送り、`publishAt`（予約）や画面で公開する流れなら影響しない。AI の申告は `status.containsSyntheticMedia`（現実と見間違う改変・合成が対象） | **API 化の第一候補**。非公開で送り、利用者が画面で公開 |
| Instagram | できる（プロアカウントのみ。REELS コンテナ → 公開） | Meta の開発者アプリ。Instagram ログインで `instagram_business_basic`・`instagram_business_content_publish`。自分のアカウントだけなら Standard Access で審査不要 | 記載なし（無料とみられる） | 動画は公開 URL から取り込み（Facebook ログイン経由なら再開可能アップロードも可）。1日の投稿上限は 100（別の節では 50 と記載があり要確認）。下書き・予約の API はない＝送ると即公開 | 手動投稿。API 化するなら、承認後にだけ送る |
| TikTok | できる（Content Posting API） | 開発者アプリと `video.publish`。**監査を通るまでは自分だけに見える投稿（SELF_ONLY）・非公開アカウントのみ** | 記載なし | 投稿前に利用者がプレビューを見て、公開範囲を自分で選び、同意する画面が必要（既定値なし）。完全な無人投稿は指針に合わない | 手動投稿（ファイルと文章を用意） |
| note | 一般向けの投稿 API はない | note API は法人向けの note pro だけ（記事を下書きとして保存。公開は画面で） | note pro は有料 | — | 記事の文章と静止画を用意。動画は YouTube などに公開した後、URL を本文に貼る |

出典（検索で確認した公式ページ）:
- YouTube: [videos.insert](https://developers.google.com/youtube/v3/docs/videos/insert)、[Revision History](https://developers.google.com/youtube/data/revision_history)、[Quota and Compliance Audits](https://developers.google.com/youtube/data/guides/quota_and_compliance_audits)、[Videos リソース](https://developers.google.com/youtube/data/reference/rest/v3/videos)
- Instagram: [Content Publishing](https://developers.facebook.com/documentation/instagram-platform/content-publishing)、[IG User Media](https://developers.facebook.com/documentation/instagram-platform/instagram-graph-api/reference/ig-user/media)、[App Review](https://developers.facebook.com/documentation/instagram-platform/app-review)、[Overview](https://developers.facebook.com/documentation/instagram-platform/overview)
- TikTok: [Get Started](https://developers.tiktok.com/docs/en/content-posting-api-get-started)、[Direct Post](https://developers.tiktok.com/docs/en/content-posting-api-reference-direct-post)、[Content Sharing Guidelines](https://developers.tiktok.com/docs/en/content-sharing-guidelines)
- note: [外部サービスと連携して note の記事を作成する（note API）](https://www.help-note.com/hc/ja/articles/63304278903321)、[埋め込みできるサービス一覧](https://www.help-note.com/hc/ja/articles/360019596133)

## 4. 関門のしくみ

- **予算**: 有料の処理の前に `budget.check`（月の上限・1作品の上限）。
- **公開**: `publish/ledger.json` で話ごとの状態を記録する（preparing → awaiting_review → approved → published）。
  前の話が published（または withdrawn）になるまで、次の話の `commit`・有料の `images`・`video-trial` を止める。
- **承認**: `publish approve --sha <先頭>` は、確認待ちのパッケージの投稿版 sha256 と一致し、自動確認が合格で、
  作者ブランドが確定（status: final）のときだけ通る。パッケージを作り直すと承認は無効になる。
- **投稿**: 投稿の直前に `publish check --platform …`（承認済み・ファイル一致・未投稿）。投稿後に `publish record`。
  同じ話・同じ投稿先の二度目の記録は拒否する。将来 API で投稿する処理も、必ずこの2つを通す。
- **二重生成**: 画像・動画・書き出しは条件の指紋（key）で記録し、同じ条件なら作らず再利用する。

## 5. 費用の見込み（追加分）

| 項目 | 費用 | 備考 |
|---|---|---|
| YouTube Data API | 0円 | 割り当て内 |
| Instagram API・TikTok API | 0円（記載なし） | 開発者登録のみ |
| note | 0円（手動投稿） | note pro は使わない |
| サイト | 0円〜 | 公開するなら無料の静的ホスティングで足りる。独自ドメインは年額がかかり、月の上限に含める |
| Google ドライブ | 0円 | 無料枠（空き約 14.9GB） |
