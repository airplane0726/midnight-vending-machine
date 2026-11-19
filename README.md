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
| 画像・動画・音声の生成、SNS 投稿 | ⏳ 未接続（接続口のインターフェースのみ。下記参照） |

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
  integrations.py             画像・動画・音声・投稿の接続口（抽象クラスのみ）
  templates/episode_template.json
series/midnight-vending-machine/
  series.json                 作品設定（人物・場所・画風・形式・ルール・初期の謎）
  state.json                  履歴（自動生成。手で編集しない）
  episodes/ep001/
    episode.json              第1話の正本
    episode.md                読みやすい台本
    production.json           カットごとの合成済み画像・動画プロンプト、効果音タイムライン、投稿文
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
python3 -m unittest discover -s tests -v
```

Claude Code では `/daily-episode` と入力するか、「今日の話を作って」と頼むと、`prompts/daily_episode.md` の手順で次の話を制作します。

### 履歴を壊さないための仕組み

- **正本はエピソードファイル**。`state.json` は `series.json` の初期状態に第1話から順に適用して作る派生データなので、いつでも `rebuild` で作り直せます。
- **書き込む前に検証を終える**。既存の全話＋下書きをメモリ上で再生して検証し、失敗したら何も書きません。
- **原子的な配置**。エピソードは一時ディレクトリに書いてから rename で配置し、`state.json` は一時ファイルから置き換えます。`state.json` の書き込みに失敗した場合はエピソードの配置を取り消します（差し替え時は元の話に戻します）。
- **再実行に安全**。同じ内容の再実行は何も変えずに成功します。中断で `state.json` だけ古いまま残った場合も、同じ下書きを再実行すると修復されます。中断で残った一時ディレクトリは次回自動で片付けます。
- **話数の重複・飛びを拒否**。内容の違う同じ話数はエラー。差し替えは `--replace` で最新話のみ。
- **同時実行の防止**。連載ごとのロックファイルで、2つの処理が同時に履歴を更新しないようにしています。
- **連続性の検査**。品物の性質の変更、未登録の謎・構想・人物の参照、タイトルの重複を拒否します。

### ほかの物語に使う場合

`series/<新しいid>/series.json` を作り（形式・カット尺・必須項目・人物・場所・画風・ルール・初期状態）、すべてのコマンドに `--series series/<新しいid>` を付けます。エンジン側の変更は不要です。

## 第1話「雨のしかく」

午前0時、古い自販機の下段右端のボタンだけが光る。ポコがつま先立ちで押すと、中だけで雨が降る透明な立方体が出てくる。傾けると雨も傾き、逆さにすると雨は上へ昇ってポコも浮く。慌てて元に戻すと着地し、足元に小さな虹が残る。それを取り出し口からモチがのぞいている（ポコはまだ気づいていない）。

| カット | 尺 | 内容 |
|---|---|---|
| 1 光るボタン | 3秒 | ボタンが1つ光り、ポコが2歩近づいてつま先立ちで押す |
| 2 落ちてきた立方体 | 4秒 | 取り出し口から透明な立方体を取り出す。中で雨が降っている |
| 3 傾く雨 | 4秒 | 左右に傾けると、雨も同じ角度で傾く |
| 4 逆さの雨 | 5秒 | 逆さにすると雨が昇り、ポコが浮く。慌てて元に戻す |
| 5 小さな虹と、のぞく目 | 4秒 | 着地し、足元に小さな虹。取り出し口からモチがのぞく |

詳細は [`episodes/ep001/episode.md`](series/midnight-vending-machine/episodes/ep001/episode.md)。

## 残っている接続作業（次の段階）

各工程の入力は `episodes/epNNN/production.json` にまとまっています。サービスと費用を確認したうえで、`storyengine/integrations.py` の抽象クラスを実装します。認証情報は環境変数またはシークレット設定で渡し、リポジトリには入れません（`.env.example` 参照）。

1. **画像生成**（`ImageGenerator`）
   - サービス選定（縦9:16・キャラクター一貫性の機能があるもの）、料金・利用規約（商用利用、生成物の権利）の確認、APIキーの取得。
   - ポコとモチの基準画像（キャラクターシート）を先に作り、参照画像として固定すると外見のぶれが減ります。
   - 出力は `episodes/epNNN/assets/cutNN_start.png`（Git 管理外）。
2. **動画生成**（`VideoGenerator`）
   - 画像から動画（image-to-video）に対応し、3〜5秒・9:16 で出力できるサービスの選定と費用確認。
   - カットの開始画像を前カットの最終フレームと照合する工程（必要なら最終フレームを次カットの初期画像に使う）。
3. **音**（`AudioProducer`）
   - 音楽・効果音の調達方法の決定（生成サービス / ライセンス素材 / 自作）。`production.json` の `audio.sfx_timeline` は20秒の絶対時刻になっています。
4. **編集・書き出し**
   - 5カットの結合、音声の合成、1080×1920 / 20秒での書き出し（例: ffmpeg）。
5. **投稿**（`Publisher`）
   - **Instagram**: プロ（ビジネス/クリエイター）アカウント、Facebookページとの連携、Meta 開発者アプリ、Graph API のリール投稿権限とアクセストークン。動画は公開URLから取得される方式のため、一時的な公開ストレージも必要。
   - **YouTube**: Google Cloud プロジェクト、YouTube Data API v3、OAuth 同意画面と認証（リフレッシュトークン）、アップロードのクォータ確認。AI 生成コンテンツの開示設定の確認。
   - まずは非公開・限定公開・下書き（dry run）で動作確認してから公開に切り替える。
6. **毎日の自動実行**
   - Claude Code のスケジュール実行などで `/daily-episode` を毎日起動し、制作 → 検証 → コミットまでを自動化。生成と投稿は、上記の接続後に人の確認を挟むか段階的に自動化。
