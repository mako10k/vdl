# vdl Chrome 拡張

ローカル **vdl API**（既定 `http://127.0.0.1:18765`）と **Web console**（`http://127.0.0.1:18766`）に接続する Manifest V3 拡張です。

バックエンドとフロントが起動している前提です。

## できること

| 操作 | 内容 |
|------|------|
| **ページ内検出** | DOM（`video` / `source` / リンク / data 属性）+ Performance + **ネットワーク**（m3u8 / mp4 等） |
| **バッジ** | タブごとに検出件数を表示 |
| **ポップアップ** | 検出一覧 → 選択して DL 投入 / **Console で開く** |
| **右クリック** | 検出 / Console 連携 / Enqueue / サーバー側スクレイプ |
| **Web 連携** | `POST /api/bridge/push` → Console が `?vdl_bridge=TOKEN&tab=candidates` で取り込み |
| **投入後** | Console の **Queue** タブを自動で開く |

## インストール

1. API + Web を起動  
   ```bash
   # API
   python -m uvicorn api.main:app --host 127.0.0.1 --port 18765
   # UI
   cd web && npm run dev   # http://127.0.0.1:18766
   ```
2. Chrome → `chrome://extensions` → デベロッパーモード ON  
3. **パッケージ化されていない拡張機能を読み込む** → この `extension/` を選択  
4. 更新後は拡張の **再読み込み**

## 典型フロー

1. 動画サイトで再生を開始（HLS が流れるのを待つ）  
2. 拡張アイコンのバッジがカウントアップ  
3. ポップアップ → **選択を DL 投入**（Queue が開く）  
   または **Console で開く** → Candidates タブで確認して Enqueue  

## 権限

- `webRequest` + `http(s)://*/*` — ページ内メディア URL の観測  
- `scripting` / content scripts — DOM スキャン  
- `127.0.0.1:18765/18766` — API / UI  

## ファイル

```
extension/
  manifest.json
  background.js   # 検出ストア・webRequest・メニュー・bridge
  content.js      # DOM / Performance スキャン
  media.js        # URL 分類
  api.js
  popup.*
  options.*
  icons/
```

## 制限

- **MPEG-DASH (`.mpd`)** は検出はするが投入は無効（バックエンド未対応）  
- HLS の **セグメント `.ts`** は無視し、**`.m3u8` プレイリスト**を優先  
- **画像**（`.jpg` / `.png` 等）は `type="video/*"` でも候補にしない  
- レジストリ登録済み・キュー中は **既定チェック OFF**（`selected_by_default`）  
- `chrome://` 等の制限ページでは検出不可  
- blob: / MSE のみのプレイヤーは URL が取れない場合あり（ネットワークに m3u8 が出れば捕捉）
