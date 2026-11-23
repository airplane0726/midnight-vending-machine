# 午前0時の自販機 — 連載ショート動画の自動制作基盤

オリジナル連載「午前0時の自販機」を、毎日、前回の内容を引き継いで制作するための土台です。
最終目標は「台本 → 画像 → 動画 → 投稿文 → Instagram / YouTube への投稿」までの自動化です。

## 今回実装した範囲

| 項目 | 状態 |
|---|---|
| 連載設定（前提・形式・人物と場所の外見・画風・制作ルール）のファイル管理 | ✅ `series/midnight-vending-machine/series.json` |
| 履歴（確定した出来事・登場した品物と性質・人物の状態・関係・未回収の謎・次の話数）の管理 | ✅ `state.json`（自動生成） |
| 将来の構想を、実際に起きた出来事と分けて保存 | ✅ `state.json` の `future_ideas_non_canon` |
| 第1話「雨のしかく」の台本（JSON と Markdown） | ✅ `episodes/ep001/episode.json` / `episode.md` |
| 5カット分の画像生成指示・動画生成指示・音の指示 | ✅ 各カット + 合成済みの `episodes/ep001/production.json` |
| Instagram 投稿文、YouTube タイトル・説明文・タグ | ✅ `episode.json` の `posts` |
| 検証プログラム（5カット・各尺・合計20秒・必須項目・話数重複など） | ✅ `python3 -m storyengine validate` |
| 検証成功後の履歴更新、失敗時・再実行時の保護 | ✅ `python3 -m storyengine commit` |
| 次回以降の Claude Code 用の指示 | ✅ `CLAUDE.md`、`prompts/daily_episode.md`、`/daily-episode` |
| 作品固有の設定と処理の分離 | ✅ `storyengine/`（汎用）と `series/<id>/`（作品固有） |
| 画像生成の準備（参照画像、生成記録、再利用判定、途中再開、dry-run） | ✅ `python3 -m storyengine images`（生成サービスは未接続） |
| OpenAI 画像生成（GPT-Image-2.6 Sunburst、参照画像つき）の接続処理 | ✅ 実装・テスト済み（`--allow-paid` を付けたときだけ有料生成。API キー登録は下記） |
| 動画・音声の生成、素材の永続保存、SNS 投稿 | ⏳ 未接続（接続口のインターフェースのみ。下記参照） |

外部サービスへの接続・有料APIの呼び出し・SNSへの公開は一切行っていません。

## ディレクトリ構成

```
storyengine/                  作品に依存しない処理（標準ライブラリのみ、Python 3.10+）
  cli.py                      コマンド（brief / scaffold / validate / commit / render / rebuild / status）
  validate.py                 検証（数値・必須項目は series.json の format から読む）
  state.py                    履歴の構築（保存済みの話を初期状態に順に適用）
  commit.py                   登録処理（原子的書き込み、ロールバック、冪等性、ロック）
  render.py                   episode.md と production.json の生成（外見プロンプトの自動合成）
  storage.py                  原子的なファイル書き込み、排他ロック、内容ハッシュ
  media.py                    画像の生成記録・条件キー・再利用判定・画像の検査・生成処理
  integrations.py             画像・動画・音声・投稿・永続保存の接続口（抽象クラスと登録表）
  openai_images.py            OpenAI Images API の画像生成器（provider: openai）
  google_drive.py             Google ドライブへの永続保存（provider: google-drive、Drive API v3）
  templates/episode_template.json
series/midnight-vending-machine/
  series.json                 作品設定（人物・場所・参照画像・画風・形式・ルール・初期の謎・生成/保存先の設定）
  state.json                  履歴（自動生成。手で編集しない）
  references/                 人物・場所の基準画像（Git 管理。置き方は references/README.md）
  media/epNNN/records.json    生成記録（Git 管理）
  media/epNNN/images/         生成した画像（Git 管理外。永続保存は asset_storage）
  episodes/ep001/
    episode.json              第1話の正本
    episode.md                読みやすい台本
    production.json           カットごとの合成済み画像・動画プロンプト、参照画像、保存先、効果音タイムライン、投稿文
  drafts/                     下書き（Git 管理外）
prompts/daily_episode.md      毎日の制作手順
.claude/commands/daily-episode.md   スラッシュコマンド /daily-episode
CLAUDE.md                     Claude Code 向けの常設指示
tests/test_storyengine.py     テスト
.env.example                  次の段階で必要になる環境変数の「名前」だけ
```

## 使い方

```bash
python3 -m storyengine status        # 登録済みの話数、次の話数、state.json の整合
python3 -m storyengine brief         # 次の話を書くための設定・履歴の要約
python3 -m storyengine scaffold      # drafts/epNNN.json にひな形を作成
python3 -m storyengine validate series/midnight-vending-machine/drafts/ep002.json
python3 -m storyengine commit   series/midnight-vending-machine/drafts/ep002.json
python3 -m storyengine images --dry-run          # 最新話の画像生成の対象・参照画像・保存先・再利用/生成の判定（何も書かない）
python3 -m storyengine images --episode 1 --cuts 1 --allow-paid   # 有料生成（見積もり確認後のみ。未接続なら何も生成しない）
python3 -m storyengine persist                   # 完成済みでまだ永続保存されていない画像を保存先へ送る（保存後にダウンロードして照合）
python3 -m storyengine restore --check           # 保存先の全画像をダウンロードして照合（生成はしない）
python3 -m storyengine import-zip <zip>          # 記録と一致する画像だけを ZIP から手元に戻す
python3 -m storyengine drive-token begin --mode google   # Actions に渡す依頼（依頼ID・公開鍵）を作る
python3 -m storyengine drive-token finish --request-id <ID> --log <ログ> --then persist --episode 1
python3 -m unittest discover -s tests -v
```

前のカットで採用した画像を次のカットの参照にするときは、`episode.json` のカットに
`"continuity_references": [{"slot": "cut01_start", "prompt_note": "..."}]` を書きます。参照先の画像が更新されると、そのカットは「更新が必要」になります。

Claude Code では `/daily-episode` と入力するか、「今日の話を作って」と頼むと、`prompts/daily_episode.md` の手順で次の話を制作します。

### 履歴を壊さないための仕組み

- **正本はエピソードファイル**。`state.json` は `series.json` の初期状態に第1話から順に適用して作る派生データなので、いつでも `rebuild` で作り直せます。
- **書き込む前に検証を終える**。既存の全話＋下書きをメモリ上で再生して検証し、失敗したら何も書きません。
- **原子的な配置**。エピソードは一時ディレクトリに書いてから rename で配置し、`state.json` は一時ファイルから置き換えます。`state.json` の書き込みに失敗した場合はエピソードの配置を取り消します（差し替え時は元の話に戻します）。
- **再実行に安全**。同じ内容の再実行は何も変えずに成功します。中断で `state.json` だけ古いまま残った場合も、同じ下書きを再実行すると修復されます。中断で残った一時ディレクトリは次回自動で片付けます。
- **話数の重複・飛びを拒否**。内容の違う同じ話数はエラー。差し替えは `--replace` で最新話のみ。
- **同時実行の防止**。連載ごとのロックファイルで、2つの処理が同時に履歴を更新しないようにしています。
- **連続性の検査**。品物の性質の変更、未登録の謎・構想・人物の参照、タイトルの重複を拒否します。

## 素材の保存場所とパスの基準

`production.json`、生成記録、`series.json` の `reference_images` に書く素材のパスは、**すべて連載ディレクトリ（`series/<id>/`）からの相対パス**です（`production.json` の `paths_relative_to: "series_dir"`）。

| 種類 | 場所 | Git | 備考 |
|---|---|---|---|
| 参照画像（基準画像） | `references/characters/<id>/`、`references/locations/<id>/` | 管理する | 小さなファイル（目安 1MB 以下）。`series.json` の `reference_images` に登録 |
| 生成した画像 | `media/epNNN/images/<スロット>-<条件キー先頭16桁>.<拡張子>` | 管理しない | 条件ごとに別名で保存するので、条件が変わっても以前の画像は残る |
| 生成した動画（予定） | `media/epNNN/videos/` | 管理しない | |
| 生成記録 | `media/epNNN/records.json` | 管理する | 条件キー、条件の内訳、シード、ファイルの SHA-256、大きさ、永続保存の状態、失敗履歴 |

- **エピソードの差し替え（`commit --replace`）は `media/` に触れません。** エピソードのディレクトリに旧形式の `assets/` などがあれば、それも引き継ぎます。
- **クラウド環境の終了後も素材を残すには、永続保存先（`series.json` の `asset_storage`）が必要です。** 未設定の間は、生成した画像はその環境にしか残らず、`images` コマンドは「保存完了ではありません」と表示します。永続保存先へのアップロードに成功したものだけを「保存済み」とし、記録のある画像がローカルから消えていれば保存先から復元して再利用します。

### 画像の生成条件と再利用

- 生成条件 = 合成済みの指示文、除外指示、モデル（`generation.image` の provider / model / params）、縦横比と解像度、参照画像（パスと内容の SHA-256）。この内容から条件キーを作ります。
- シード値は、生成器が対応している場合だけ、条件キーから決まる固定値を渡して記録します。
- 同じ条件キーで完成した記録があり、ファイルが**正常な画像として読み込め**（PNG は CRC と画素データまで検査）、縦横比と内容（SHA-256）が記録どおりなら再利用します。
- 生成に失敗した出力、壊れた画像、縦横比が違う画像は「失敗」として記録し、完成品として扱いません。
- 5カットを順に処理し、1カットごとに記録を保存します。途中で失敗したら止まり、再実行すると成功済みのカットを再利用して続きから再開します。
- プレースホルダー（ダミー）の生成器の出力は登録しません。生成サービスが未接続なら何も生成しません。

### Google ドライブの認証（GitHub Actions 経由）

リフレッシュトークンは GitHub Actions のシークレットにだけ置き、このクラウド環境には置きません。
`.github/workflows/drive-token.yml` が1時間有効のアクセストークンを発行し、依頼ごとに作る公開鍵で暗号化してジョブログに出力します。

- 画像・動画は GitHub を通らず、クラウド環境から Google ドライブへ直接送ります（Git の履歴に素材が残らない）。
- 暗号方式: 64バイトの鍵を RSA-OAEP(SHA-256, 3072bit) で包み、本文は AES-256-CBC、改ざん検出は HMAC-SHA256（依頼 ID・発行時刻・モードも検証対象）。
- ワークフローは権限なし（`permissions: {}`）、外部アクション・チェックアウトなし、入力は env 経由のみ。シークレットは `mode: google` のときだけ渡す。
- 依頼 ID を実行名と暗号文の両方に入れ、別の依頼の結果を受け付けない。発行から15分を過ぎた結果も受け付けない。
- 成功・失敗にかかわらず、秘密鍵とログファイルを削除する。平文のトークンはメモリ上だけで使う。
- `mode: dummy` はシークレットを使わず同じ方式で長いダミー値を返す（起動・ログ取得・復号の確認用）。

### ほかの物語に使う場合

`series/<新しいid>/series.json` を作り（形式・カット尺・必須項目・人物・場所・画風・ルール・初期状態）、すべてのコマンドに `--series series/<新しいid>` を付けます。エンジン側の変更は不要です。

## 第1話「雨のしかく」

午前0時、古い自販機の下段右端のボタンだけが光る。ポコがつま先立ちで押すと、中だけで雨が降る透明な立方体が出てくる。傾けると雨も傾き、逆さにすると雨は上へ昇ってポコも浮く。慌てて元に戻すと着地し、足元に小さな虹が残る。それを取り出し口からモチがのぞいている（ポコはまだ気づいていない）。

| カット | 尺 | 内容 |
|---|---|---|
| 1 光るボタン | 3秒 | ボタンが1つ光り、ポコが画面右へ2歩進んでボタンの真下でつま先立ちで押す |
| 2 落ちてきた立方体 | 4秒 | 足元の取り出し口から透明な立方体を取り出し、カメラの方へ向き直る。中で雨が降っている |
| 3 傾く雨 | 4秒 | 左右に傾けると、雨も同じ角度で傾く |
| 4 逆さの雨 | 5秒 | 逆さにすると雨が昇り、ポコが浮く。慌てて元に戻す |
| 5 小さな虹と、のぞく目 | 4秒 | 着地し、足元に小さな虹。取り出し口からモチがのぞく |

詳細は [`episodes/ep001/episode.md`](series/midnight-vending-machine/episodes/ep001/episode.md)。

## 残っている接続作業（次の段階）

各工程の入力は `production.json` と同じ内容です（`images` コマンドは常に `episode.json` と現在の `series.json` から組み立てるので、`production.json` が古くても最新の指示で生成します）。サービスと費用を確認したうえで、`storyengine/integrations.py` の抽象クラスを実装して登録表（`IMAGE_GENERATORS` / `ASSET_STORES`）に追加し、`series.json` で選びます。認証情報は環境変数またはシークレット設定で渡し、リポジトリには入れません（`.env.example` 参照）。

1. **参照画像**：✅ 登録済み（ポコ・モチ・自販機と路地）。
2. **画像生成（OpenAI）**
   - ✅ 実装済み：`storyengine/openai_images.py`。`series.json` の `generation.image` は `gpt-image-2.6-sunburst`、`1152x2048`（9:16）、`quality: high`。
   - ✅ 実行環境：作品専用のクラウド環境「midnight-vending-machine」で `api.openai.com` と公式資料への接続を許可済み。
   - ⏳ API キー：環境のネットワーク用シークレットとして登録する（リポジトリ・会話・環境変数には書かない）。必要なら OpenAI の組織の本人確認（Organization Verification）。
   - ⏳ 費用確認のうえ `images --dry-run --cuts 1` → `images --cuts 1 --allow-paid` で1カットだけ試す。生成記録に実際のトークン数と費用（`usage`、`cost_usd`）が残る。
   - シード値は API にないため使わない。除外指示は指示文の末尾に「Do not include: …」として付ける。
3. **永続保存（Google ドライブ）**：✅ 実装・テスト済み（`storyengine/google_drive.py`）。
   - 無料の保存容量（15GB、Gmail・フォトと共有）内で使う。スコープは `drive.file`（このアプリが作ったファイルだけを扱える非機密スコープ）。
   - OAuth クライアントの ID・シークレットはネットワーク用シークレット（`oauth2.googleapis.com`、Basic）、リフレッシュトークンは環境変数 `GOOGLE_DRIVE_REFRESH_TOKEN`（クラウド環境の設定で入力）。どちらもチャット・Git には置かない。
   - 保存前に空き容量を確認し（予備 500MB）、不足または確認できない場合は有料の画像生成を止める。
   - 保存後にダウンロードして SHA-256 を照合できたものだけ「保存済み」。手元から消えた画像は Drive から復元して再利用する。
   - ⏳ Google 側の設定（下記の手順）と、認証情報の登録。
4. **動画生成**（`VideoGenerator`）
   - 画像から動画（image-to-video）に対応し、3〜5秒・9:16 で出力できるサービスの選定と費用確認。開始画像は生成記録の完成済み画像（`init_image_slot`）を使う。
5. **音**（`AudioProducer`）
   - 音楽・効果音の調達方法の決定（生成サービス / ライセンス素材 / 自作）。`production.json` の `audio.sfx_timeline` は20秒の絶対時刻。
6. **編集・書き出し**
   - 5カットの結合、音声の合成、1080×1920 / 20秒での書き出し（例: ffmpeg）。
7. **投稿**（`Publisher`）
   - **Instagram**: プロ（ビジネス/クリエイター）アカウント、Facebookページとの連携、Meta 開発者アプリ、Graph API のリール投稿権限とアクセストークン。動画は公開URLから取得される方式のため、一時的な公開ストレージも必要。
   - **YouTube**: Google Cloud プロジェクト、YouTube Data API v3、OAuth 同意画面と認証（リフレッシュトークン）、アップロードのクォータ確認。AI 生成コンテンツの開示設定の確認。
   - まずは非公開・限定公開・下書き（dry run）で動作確認してから公開に切り替える。
8. **毎日の自動実行**
   - Claude Code のスケジュール実行などで `/daily-episode` を毎日起動し、制作 → 検証 → コミットまでを自動化。生成と投稿は、上記の接続後に人の確認を挟むか段階的に自動化。
