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
```

別の連載は `--series series/<id>` を付ける。標準ライブラリのみ（Python 3.10+）。

## 守ること

- 秘密情報（APIキー、トークン）をコード・JSON・コミットに入れない。環境変数で渡す（`.env.example` に名前だけ記載）。
- 外部APIの呼び出し、画像・動画生成、SNS投稿は、ユーザーが接続と費用を確認するまで行わない。
- 登録済みの話は書き換えない（最新話のみ `commit --replace` で差し替え可）。
- 品物の性質、人物の外見、形式は途中で変えない。変更が必要ならユーザーに確認する。
- 実際に起きた出来事（`new_facts`）と未確定の構想（`future_ideas`）を混ぜない。
- エンジンを変更したらテストを実行し、`series.json` で設定できる形を保つ。
