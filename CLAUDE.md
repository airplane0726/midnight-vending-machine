# CLAUDE.md

このリポジトリは、せりふのない縦型ショート動画の連載を毎日制作するための土台です。
現在の連載は「午前0時の自販機」（`series/midnight-vending-machine/`）。

## 毎日の制作を頼まれたら

`prompts/daily_episode.md` の手順に従う（スラッシュコマンド `/daily-episode` も同じ）。
まず `python3 -m storyengine status` と `python3 -m storyengine brief` で設定と履歴を読んでから考えること。

## 構成

- `storyengine/` … 作品に依存しない処理（検証・履歴・Markdown/制作パッケージ生成・CLI）。作品固有の値を書かない。
- `series/<id>/series.json` … 作品固有の設定（前提、形式、人物・場所の外見、画風、制作ルール、初期の謎）。
- `series/<id>/episodes/epNNN/episode.json` … 登録済みの各話（**正本**）。`episode.md` と `production.json` はここから自動生成。
- `series/<id>/state.json` … 履歴（次の話数、確定した出来事、品物、関係、謎、構想）。**自動生成。手で編集しない。** `rebuild` で作り直せる。
- `series/<id>/drafts/` … 下書き（Git 管理外）。
- `series/<id>/references/` … 人物・場所の基準画像（Git 管理）。`series.json` の `reference_images` に登録。
- `series/<id>/media/epNNN/` … 生成素材（`images/` などは Git 管理外）と生成記録 `records.json`（Git 管理）。
- 素材のパスはすべて連載ディレクトリ（`series/<id>/`）からの相対パス。
- `prompts/daily_episode.md` … 毎日の制作手順。
- `tests/` … `python3 -m unittest discover -s tests`

## コマンド

```bash
python3 -m storyengine status                 # 次の話数・履歴の整合
python3 -m storyengine brief                  # 次の話を書くための要約
python3 -m storyengine scaffold               # 下書きひな形
python3 -m storyengine validate <draft.json>  # 検証のみ
python3 -m storyengine commit <draft.json>    # 検証→登録→履歴更新（冪等）
python3 -m storyengine render [--episode N]   # episode.md / production.json 再生成
python3 -m storyengine rebuild                # state.json 再生成
python3 -m storyengine images --dry-run       # 画像生成の対象・参照画像・保存先・再利用判定（書き込みなし）
python3 -m storyengine persist                # 生成済み画像を永続保存先（Google ドライブ）へ送り、復元を確認する
python3 -m storyengine restore [--check]      # 保存先から画像を取り戻す／全件を照合（生成はしない）
python3 -m storyengine import-zip <zip>       # 記録と一致する画像だけを ZIP から戻す（新しい環境で画像がないとき）
python3 -m storyengine drive-token begin|finish|cleanup  # Actions から暗号化した Drive 用トークンを受け取る（下記）
python3 -m storyengine budget [--episode N]   # 今月の制作費（消費）・上限・前払いの入金
python3 -m storyengine animatic --episode N   # 採用画像だけで構成確認用の動画（無料）
python3 -m storyengine roughcut --episode N   # 既存素材だけで合成・音・編集を入れた仮編集（無料。edits/epNNN_roughcut.json）
python3 -m storyengine video-trial --episode N --cut C --dry-run  # カット単位の動画試作（技術確認）
```

別の連載は `--series series/<id>` を付ける。標準ライブラリのみ（Python 3.10+）。

## 予算（budget/policy.json）

- 3日に1本。月の上限 3,000円（画像・動画・台本・音楽・編集・保存・投稿、失敗・作り直しを含む。既存の月額契約は別）。
- 1本は原則 300円以内。月の上限が常に優先。超える依頼は送らない（コードで確認している）。
- 前払いの入金（budget/ledger.json）と実際の消費（生成記録の cost_usd）を分けて記録する。入金額は事前に利用者が確認する。自動入金は OFF。
- 有料の処理の前に `budget` で残りを確認し、見積もりを利用者に示してから `--allow-paid` を付ける。自動の作り直しはしない。
- 制作は自動化してよいが、投稿は利用者が確認してから行う。

## 守ること

- 秘密情報（APIキー、トークン）をコード・JSON・コミットに入れない。環境変数で渡す（`.env.example` に名前だけ記載）。
- 外部APIの呼び出し、画像・動画生成、SNS投稿は、ユーザーが接続と費用を確認するまで行わない。`images` は `--dry-run` のみ使い、`--allow-paid` はユーザーがその回の見積もりを確認してから付ける。
- OpenAI の API キーはクラウド環境のネットワーク用シークレットに保存されている（通信経路で付与）。キーをファイル・コミット・会話に書かない。
- Google ドライブのリフレッシュトークンは GitHub Actions のシークレットにだけ置く。クラウド環境の環境変数・ファイル・会話に置かない。値を表示・記録しない。保存先の空き容量が確認できないときは有料生成をしない。
- Google ドライブのトークンは `drive-token` で受け取る: `begin --mode google` → GitHub 連携で `drive-token.yml` を main 上で起動（入力は request_id・public_key・mode）→ 実行名「drive-token <request_id>」の実行のジョブログを取得してファイルに保存 → `finish --request-id … --log … --then <コマンド>`。トークンは表示・保存しない。
- ダミー画像を生成素材として登録しない。永続保存先が未設定なら「保存完了」と報告しない。
- 左右は「画面左／画面右」と「ポコ自身の右腕・左腰」を区別して書く。
- 登録済みの話は書き換えない（最新話のみ `commit --replace` で差し替え可）。
- 品物の性質、人物の外見、形式は途中で変えない。変更が必要ならユーザーに確認する。
- 実際に起きた出来事（`new_facts`）と未確定の構想（`future_ideas`）を混ぜない。
- エンジンを変更したらテストを実行し、`series.json` で設定できる形を保つ。
