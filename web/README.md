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

# API と UI を同時に起動（推奨）
npm run dev:all
```

- ブラウザ: http://127.0.0.1:18766  
- API docs: http://127.0.0.1:18765/docs  

停止は `Ctrl+C` です。`dev:all` は API を `--reload` で起動するため、バックエンドと UI の変更を開発中に反映します。対象ポートで VDL API がすでに稼働していれば、それを再利用し、停止しません。

開発時、Vite は `/api` を `VDL_API_PORT`（既定 18765）へプロキシします。

### 個別起動

API を別ターミナルや別プロセスで管理したい場合のみ、次のように起動します。

```bash
# ターミナル1: API
uvicorn api.main:app --reload --host 127.0.0.1 --port 18765

# ターミナル2: UI
cd web && npm run dev
```

UI 上部に `API · unavailable` と表示された場合は API が未起動、または API/UI 間で `VDL_API_PORT` が一致していません。`runtime · idle (auto-start)` はキューが空の正常な待機状態で、ジョブ投入時に worker が自動起動します。

## 機能

| タブ | 内容 |
|------|------|
| Get | URL をダウンロードキューへ |
| Candidates | HTML スクレイプ → 選択して enqueue |
| Queue | ジョブ進捗（2秒ポーリング） |
| Registry | ダウンロード一覧・再索引・類似 |
| Search | テキスト検索 / 類似検索 |
