# 参照画像（基準画像）

人物・場所の見た目を話をまたいで固定するための基準画像を置く場所です。Git で管理します。

## 置き方

```
references/characters/<人物ID>/<ファイル名>.png   例: characters/poco/poco_reference.png
references/locations/<場所ID>/<ファイル名>.png   例: locations/vending-corner/vending_corner_reference.png
```

置いたら `series.json` の該当する人物・場所の `reference_images` に登録します（パスは連載ディレクトリからの相対パス）。

```json
"reference_images": [
  {"path": "references/characters/poco/poco_reference.png", "view": "turnaround: front / side / back",
   "note": "日本語の説明（人が読む用）", "prompt_note": "English note added to generation prompts"}
]
```

- 登録した参照画像は、そのカットに映る人物・場所に応じて `production.json` と画像生成に自動で渡されます。
- 参照画像の中身が変わると、それを使うカットは「更新が必要」になります（以前の生成画像は残ります）。
- 登録したのにファイルがない場合、そのカットは生成されません（`images --dry-run` で確認できます）。
- 基準画像は原寸のまま保存します（現在 1〜3MB 程度の PNG）。GitHub は 1ファイル 50MB 超で警告、100MB 超は不可なので、それより十分小さく保ってください。動画などの大きな生成素材は `media/` と永続保存先へ。
- 人物の基準画像に複数の向きが並んでいるのは、同じ人物1体の外見確認用です。`prompt_note` にその旨を書き、生成指示には各人物を1体ずつだけ描く指示が自動で入ります。
- 文章の外見説明と基準画像が食い違う場合、生成指示では基準画像を優先します（`style.reference_prompt_intro`）。
- 登録がない間（`reference_images: []`）は、文章の外見指示だけで生成します。
