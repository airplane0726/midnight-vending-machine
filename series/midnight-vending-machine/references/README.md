# 参照画像（基準画像）

人物・場所の見た目を話をまたいで固定するための基準画像を置く場所です。Git で管理します。

## 置き方

```
references/characters/<人物ID>/<向き>.png     例: characters/poco/front.png
references/locations/<場所ID>/<向き>.png      例: locations/vending-corner/wide.png
```

置いたら `series.json` の該当する人物・場所の `reference_images` に登録します（パスは連載ディレクトリからの相対パス）。

```json
"reference_images": [
  {"path": "references/characters/poco/front.png", "view": "front", "note": "正面・通常の目"}
]
```

- 登録した参照画像は、そのカットに映る人物・場所に応じて `production.json` と画像生成に自動で渡されます。
- 参照画像の中身が変わると、それを使うカットは「更新が必要」になります（以前の生成画像は残ります）。
- 登録したのにファイルがない場合、そのカットは生成されません（`images --dry-run` で確認できます）。
- 1枚あたり 1MB 程度まで（長辺 1024px 程度の PNG）を目安にしてください。大きな生成素材は `media/` と永続保存先へ。
- 登録がない間（`reference_images: []`）は、文章の外見指示だけで生成します。
