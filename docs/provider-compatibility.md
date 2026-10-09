# OpenAI Images API 互換性確認

最終確認日: 2026-10-09

## 確認した公式資料

- [GPT-Image-2 model](https://developers.openai.com/api/docs/models/gpt-image-2)
- [Images API: generate](https://developers.openai.com/api/reference/python/resources/images/methods/generate)
- [Images API: edit](https://developers.openai.com/api/reference/python/resources/images/methods/edit)
- [Image generation guide](https://developers.openai.com/api/docs/guides/image-generation)
- [OpenAI Python SDK](https://github.com/openai/openai-python)
- [API pricing](https://developers.openai.com/api/docs/pricing)

## MVPで使用する仕様

- MVPの許可モデルは仕様書どおり`gpt-image-2`のみとする。公式資料でモデルと`v1/images/generations`、`v1/images/edits`の対応を確認した。利用可能な別モデルがあってもMVPへ追加しない。
- 参照画像なしの生成はPython SDKの`client.images.generate(...)`、参照画像付き生成と編集は`client.images.edit(...)`を使用する。参照画像付き生成は業務記録上`operation=generate`とし、編集元候補がある編集は`operation=edit`として区別する。
- 公式APIの候補数は1〜10。画像編集は最大16入力画像で、各入力はPNG/JPEG/WebP、50MB未満。プロンプト上限は32,000文字。生成結果はbase64画像データとして返るため、短期URLに依存しない。
- マスクはPNG、4MB未満、編集元と同じ寸法で、アルファチャンネルを含む必要がある。複数画像時は先頭画像にマスクが適用され、マスクは編集範囲を厳密には保証しない。
- API全体にはより広いオプションがあるが、MVPでは仕様書記載のサイズ（`1024x1024`、`1536x1024`、`1024x1536`、`auto`）、品質（`low`、`medium`、`high`）、形式（`png`、`jpeg`、`webp`）に限定する。非対応値を自動置換しない。
- OpenAI Python SDKは既定で複数種の失敗を自動再試行する。仕様に従いProvider初期化時にSDKの自動再試行を無効化し、明確なHTTP 429だけをアプリ側で制御する。
- 料金ページは画像・テキストのトークン単価と料金計算ツールを案内している。料金はコードへ固定せず、確認日付きの根拠と算定条件が揃わない場合は概算不明と表示する。

## 未確認事項と差分

- APIキーは環境に設定されていないため、ユーザーのOrganizationにおける`gpt-image-2`利用権限、利用枠、Organization verification要否は未確認。ライブAPI呼び出しは行っていない。
- アカウント固有の権限確認は、API実行を計画する段階で明示承認を得てから行う。権限不足時に別モデルへ自動変更しない。
- 2026-10-09確認時点で仕様書の固定値と公式APIの現行記載に、MVP実装を妨げる不一致は見つからなかった。公式資料の更新時は本書と仕様書の差分を再確認する。
