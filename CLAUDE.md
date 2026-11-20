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
```

別の連載は `--series series/<id>` を付ける。標準ライブラリのみ（Python 3.10+）。

## 守ること

- 秘密情報（APIキー、トークン）をコード・JSON・コミットに入れない。環境変数で渡す（`.env.example` に名前だけ記載）。
- 外部APIの呼び出し、画像・動画生成、SNS投稿は、ユーザーが接続と費用を確認するまで行わない。`images` は `--dry-run` のみ使い、`--allow-paid` はユーザーがその回の見積もりを確認してから付ける。
- OpenAI の API キーはクラウド環境のネットワーク用シークレットに保存されている（通信経路で付与）。キーをファイル・コミット・会話に書かない。
- Google ドライブは、OAuth クライアント情報をネットワーク用シークレット、リフレッシュトークンを環境変数 GOOGLE_DRIVE_REFRESH_TOKEN で受け取る。値を表示・記録しない。保存先の空き容量が確認できないときは有料生成をしない。
- ダミー画像を生成素材として登録しない。永続保存先が未設定なら「保存完了」と報告しない。
- 左右は「画面左／画面右」と「ポコ自身の右腕・左腰」を区別して書く。
- 登録済みの話は書き換えない（最新話のみ `commit --replace` で差し替え可）。
- 品物の性質、人物の外見、形式は途中で変えない。変更が必要ならユーザーに確認する。
- 実際に起きた出来事（`new_facts`）と未確定の構想（`future_ideas`）を混ぜない。
- エンジンを変更したらテストを実行し、`series.json` で設定できる形を保つ。
