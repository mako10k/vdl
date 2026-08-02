# vdl web console

Vite + React フロントエンドと FastAPI バックエンド。

## ポート（既定）

使用中ポートを避けるため、よくある `5173` / `8000` は使いません。

| 役割 | 既定ポート | 環境変数 |
|------|-----------|----------|
| API (FastAPI) | **18765** | `VDL_API_PORT` |
| UI (Vite) | **18766** | `VDL_WEB_PORT` |

`web/.env` またはシェルで変更できます。

## 起動

```bash
# 依存
cd /path/to/video
pip install -e ".[web]"
cd web && npm install

# ターミナル1: API
cd /path/to/video
export VDL_HOME=~/.vdl   # 任意
export VDL_API_PORT=18765
uvicorn api.main:app --reload --host 127.0.0.1 --port 18765
# または: vdl-api

# ターミナル2: UI
cd web
npm run dev
```

- ブラウザ: http://127.0.0.1:18766  
- API docs: http://127.0.0.1:18765/docs  

開発時、Vite は `/api` を `VDL_API_PORT`（既定 18765）へプロキシします。

## 機能

| タブ | 内容 |
|------|------|
| Get | URL をダウンロードキューへ |
| Candidates | HTML スクレイプ → 選択して enqueue |
| Queue | ジョブ進捗（2秒ポーリング） |
| Registry | ダウンロード一覧・再索引・類似 |
| Search | テキスト検索 / 類似検索 |
