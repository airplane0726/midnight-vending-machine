# Instagram 自動投稿の準備（2026-11-27 時点）

目標: 「完成動画を利用者が承認 → 自動投稿 → 投稿 URL を記録」。追加費用は 0 円（月 3,000 円の予算は変えない）。

## しくみ

```
利用者が承認（publish approve、投稿版の sha256 に紐付け）
  ↓
GitHub Actions「instagram-publish」を手動で起動（入力: 話数・sha・段階・依頼ID）
  1. 承認台帳を確認（承認済み・sha 一致・未投稿でなければ止める）
  2. Google ドライブから投稿版を取り寄せ、承認した中身と sha256 で照合
  3. 投稿版と表紙に、一時的な公開リンクを付ける
  4. Instagram にリールのコンテナを作る（media_type=REELS、video_url、caption、cover_url）
  5. 取り込み完了（FINISHED）まで1分ごとに確認（ERROR・期限切れなら作り直さずに止める）
  6. 段階が publish のときだけ公開（media_publish）し、投稿 URL を取得
  7. 公開リンクを必ず外す
  ↓
結果の1行（INSTAGRAM_RESULT）をこちらで読み取り、承認台帳に記録（instagram record）。二重記録は拒否
```

段階（mode）:
- `check` … 確認だけ。Instagram へは読み取りの問い合わせだけで、何も送らない
- `container` … 取り込みまで。**公開しない**（Meta が動画を取り込めるかの確認用。コンテナは24時間で消える）
- `publish` … 公開まで（利用者が「投稿してよい」と伝えたときだけ起動）

## 公式資料で確認した条件（検索で確認。接続時に公式ページで再確認する）

| 項目 | 調査時点の記載 |
|---|---|
| アカウント | Instagram のプロアカウント（クリエイターまたはビジネス）が必要 |
| ログイン方式 | Instagram ログイン（graph.instagram.com）。権限 `instagram_business_basic`・`instagram_business_content_publish` |
| 審査 | 自分の（または管理する）プロアカウントだけなら Standard Access で、アプリ審査は不要 |
| 動画の渡し方 | Instagram ログインでは公開 URL（video_url）。動画を直接送る方式は Facebook ログイン（Facebook ページの連携）のときだけ |
| 取り込み | コンテナの status_code（FINISHED・IN_PROGRESS・ERROR・EXPIRED・PUBLISHED）を1分ごと・最大5分程度確認 |
| 上限 | コンテナは24時間で期限切れ。24時間あたりの投稿数に上限（content_publishing_limit で確認。記載は 50 と 100 が混在） |
| トークン | 長期トークンは60日有効。発行から24時間以上たてば refresh_access_token で延長できる |
| 費用 | 料金の記載なし |

出典: [Content Publishing](https://developers.facebook.com/documentation/instagram-platform/content-publishing)、[IG User Media](https://developers.facebook.com/documentation/instagram-platform/instagram-graph-api/reference/ig-user/media)、[IG Container](https://developers.facebook.com/documentation/instagram-platform/instagram-graph-api/reference/ig-container)、[Business Login for Instagram](https://developers.facebook.com/documentation/instagram-platform/instagram-api-with-instagram-login/business-login)、[Refresh Access Token](https://developers.facebook.com/docs/instagram-platform/reference/refresh_access_token/)、[App Review](https://developers.facebook.com/documentation/instagram-platform/app-review)

## 未確認・注意

- Google ドライブの公開リンク（drive.usercontent.google.com）から Meta が動画を取り込めるかは未確認。最初に `container` 段階で確かめる。
  取り込めない場合は、Facebook ページを連携して直接送る方式（Facebook ログイン）に切り替える（コードの追加が必要）。
- 開発中（Development モード）のアプリから、自分のアカウントへ公開投稿できるかも、`container` の後に確かめる。
- トークンの延長（60日ごと）は、当面は利用者が新しいトークンを発行して GitHub のシークレットを差し替える。
  自動化するには GitHub のシークレットを書き換える権限が別に要るため、後で検討する。
- この環境（クラウドのコンテナ）からは Instagram に接続できないため、送信は GitHub Actions から行う。

## 利用者の操作（1つずつ）

1. Instagram アプリで @moyrr_s をプロアカウント（クリエイター）に切り替える。
2. Meta for Developers に開発者として登録し、アプリを作る（用途: Instagram のコンテンツ管理）。
3. アプリの「Instagram ログインによる API 設定」で @moyrr_s を追加し、アクセストークンを発行する（権限: instagram_business_basic・instagram_business_content_publish）。
4. GitHub の Actions シークレットに `INSTAGRAM_ACCESS_TOKEN` と `INSTAGRAM_USER_ID` を登録する（値は会話に貼らない）。
5. このブランチを main に取り込む（ワークフローは main にあるときだけ起動できる）。
6. `check` → `container` の順に起動して確認する。
7. 投稿してよいと伝えた後で `publish` を起動し、結果を記録する。
