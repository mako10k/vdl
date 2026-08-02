# vdl web console

Vite + React フロントエンドと FastAPI バックエンド。

## 起動

```bash
# 依存
cd /path/to/video
pip install -e ".[web]"
cd web && npm install

# ターミナル1: API
cd /path/to/video
export VDL_HOME=~/.vdl   # 任意
uvicorn api.main:app --reload --port 8000

# ターミナル2: UI
cd web
npm run dev
```

ブラウザ: http://127.0.0.1:5173  
API ドキュメント: http://127.0.0.1:8000/docs  

開発時、Vite は `/api` を `8000` にプロキシします。

## 機能

| タブ | 内容 |
|------|------|
| Get | URL をダウンロードキューへ |
| Candidates | HTML スクレイプ → 選択して enqueue |
| Queue | ジョブ進捗（2秒ポーリング） |
| Registry | ダウンロード一覧・再索引・類似 |
| Search | テキスト検索 / 類似検索 |
