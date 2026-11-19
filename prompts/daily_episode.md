# 毎日の制作手順（Claude Code 用）

対象の連載ディレクトリを `SERIES` とします（既定: `series/midnight-vending-machine`）。
別の連載を作るときは、すべてのコマンドに `--series <ディレクトリ>` を付けてください。

この手順では **外部APIの呼び出し、画像・動画の生成、SNSへの投稿を行いません**。
台本・生成指示・音の指示・投稿文を作り、検証して履歴に登録し、Git に保存するまでが範囲です。

---

## 1. 状態を確認する

```bash
python3 -m storyengine status
```

- `state.json: 保存済みエピソードと一致` なら次へ。
- 不一致なら `python3 -m storyengine rebuild` を実行してから次へ（state.json は保存済みエピソードから作り直せます）。
- 「別の処理が実行中です」と出たら、他の実行が終わるまで待つ。ロックファイルを手で消さない。
- `HistoryError`（保存済みの話に矛盾）が出たら、制作を中止して内容を報告する。

## 2. 設定と履歴を読む

```bash
python3 -m storyengine brief
```

出力をすべて読む。さらに次も読む:

- `SERIES/series.json`（人物・場所の外見、制作ルール、形式）
- 直前の話の `SERIES/episodes/epNNN/episode.md`（終わりの状況、各カットの位置関係）
- 必要なら過去の話の `episode.json`

## 3. 次の話を考える

brief の「次は第N話」の N を作る。以下を決めてから書き始める。

1. **品物**：新しい不思議な品物を1つ（登場済みの品物名・ID と重ねない）。見た目と大きさ、性質（properties）を決める。性質は以後変更できないので、試したときの結果と矛盾しない最小限の言葉で書く。
   - 既出の品物を再登場させる場合は、`featured_object` に登録済みの `id`・`name`・`properties` をそのまま書き、`first_appearance: false` にする（ただし、その話の主役となる新しい品物を優先する）。
2. **試し方と結果**：ポコがどう試し、何が起きるか。必ず結果まで描く。
3. **連続性**：前話の `end_state` と、ポコの持ち物（過去の品物を持ち続けているか等）、モチとの関係を踏まえる。新しい夜の午前0時から始めるのが基本。
4. **謎の扱い**：未回収の謎のうち、触れるなら1つだけ、小さく進める（`mysteries_progressed`）。大きく解決しない。
5. **構想**：brief の「今後の構想」は未確定のアイデア。採用したら `future_ideas_retired` にIDを入れ、実際に起きたことは `new_facts` に書く。新しい構想は `future_ideas` に追加する。
6. **最後のカット**：もう一度見たくなる余韻か、次が気になる小さなフック。

## 4. 下書きを作る

```bash
python3 -m storyengine scaffold
```

`SERIES/drafts/epNNN.json` にひな形ができる。すべての `TODO` を埋める。書き方は第1話（`SERIES/episodes/ep001/episode.json`）を見本にする。

各カットに必ず書くもの:

- `start_image`：開始画像の具体的な描写（人物の位置・向き・姿勢、小道具の位置と状態、光、カメラから見た配置）
- `motion`：開始画像からの動きを、秒単位（0〜カット尺）で
- `continuity.carry_in` / `carry_out` / `footing` / `props`：前後のつながりと足の接地
- `sound.ambience` / `sound.sfx`（秒付き）/ `sound.music`
- `image_prompt` / `video_prompt`（英語）。人物・場所の外見と画風は `series.json` から自動で合成されるので、ここでは構図・姿勢・動き・光に集中する
- カットの途中で登場する人物は `characters_in_shot` に入れ、開始画像にいない人物は `characters_in_start_image` から外す

使わないもの: `dialogue` / `narration` / `subtitles` / `on_screen_text`。画面内の読める文字も描かない。

## 5. 検証する（成功するまで直す）

```bash
python3 -m storyengine validate series/midnight-vending-machine/drafts/epNNN.json
```

検証されること: 5カット、各カットの尺（3・4・4・5・4秒）、合計20秒、必須項目、効果音・動きの時刻範囲、未登録の人物・場所、せりふ類の禁止、TODO の残り、投稿文の文字数とハッシュタグ、話数の重複・飛び、タイトルの重複、品物の性質の変更、謎・構想IDの整合。

検証では確認できないので、自分で必ず点検すること:

- [ ] カットNの `carry_out` とカットN+1の `start_image` / `carry_in` が、位置・向き・姿勢・小道具・足の接地で一致している
- [ ] 左右を「画面左／画面右」と「ポコ自身の右腕・左腰」で区別して書いている（ポコがカメラの方を向くと、ポコ自身の右は画面左になる）
- [ ] ポコ：クリーム色の丸い体、黒い横長の目2つ、口なし、短い腕と脚、赤いバッグ（右肩→左腰）
- [ ] ポコは歩いて移動し、浮くのは品物の作用のときだけ
- [ ] モチは取り出し口から顔や体の一部を出すだけ
- [ ] 品物の性質が話の途中で変わっていない。試した結果まで描いている
- [ ] 投稿文が結末を言いすぎていない

## 6. 登録する

```bash
python3 -m storyengine commit series/midnight-vending-machine/drafts/epNNN.json
```

- `episodes/epNNN/` に `episode.json`（正本）、`episode.md`（読みやすい台本）、`production.json`（画像・動画・音・投稿の各工程向けパッケージ）ができ、`state.json` が更新される。
- 失敗した場合、履歴は変更されない。エラーを直して再実行する。
- 同じ内容で再実行しても何も変わらない（安全）。
- 登録済みの最新話を直す必要があるときだけ `--replace` を付ける（それより前の話は差し替えできない）。

## 7. 確認して保存する

```bash
python3 -m unittest discover -s tests
python3 -m storyengine status
python3 -m storyengine images --dry-run   # 画像生成の対象と参照画像の確認のみ（生成はしない）
git add series/ && git commit -m "第N話「タイトル」を追加" && git push
```

- `state.json` は手で編集しない。
- `series.json` の人物の外見・形式・ルールは、ユーザーの指示がない限り変更しない。

## 8. 報告する

日本語で次を報告する: 話数とタイトル、あらすじ、品物と性質、5カットの要約、更新された謎・構想、検証結果、コミット。
