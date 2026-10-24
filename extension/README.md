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
| **投入後** | ポップアップ内に結果を表示。Web画面は **Console で開く** / **Console キュー** で明示的に開く |

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
3. ポップアップ → **選択を DL 投入**（Web画面は自動で開きません）
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

## 重複除去

検出一覧・サーバー候補・一括投入では同一URLを事前にまとめます。
フラグメント（`#...`）、ホスト名の大小文字、標準ポート表記の違いも同一として扱います。
クエリ文字列・パスの大小文字・HTTP/HTTPSの違いは保持します。

Edgeでは `edge://extensions` から同じ手順で読み込み・再読み込みできます。

動作テスト: `node --experimental-default-type=module --test extension/tests/*.test.mjs`

## 投入済みの除外と自動キャプチャー

- 投入済みのURLは検出一覧・候補一覧・バッジ件数から除外します。投入履歴は拡張のローカルストレージに保存し、APIの全ジョブ履歴（失敗・アーカイブを含む）とも照合します。
- ポップアップの **自動キャプチャー** は初期OFFです。ONにすると全タブで、検知対象が1秒間増えなくなった後に新規対象をまとめて投入します。同じURLの繰り返し検知は待ち時間を延ばしません。
- 手動・自動・別タブの同時投入は直列化します。APIが受理したジョブのURLだけを投入済みにします。失敗・スキップした対象は一覧に残します。
- OFFにすると待機中の自動投入を中止します。すでにAPIへ送信したリクエストは取り消しません。ON/OFFはブラウザー再起動後も保持します。
- 自動投入の通信失敗時は通知し、対象を残します。手動投入またはOFF→ONで再試行できます。APIの履歴確認ができない場合は自動投入しません。
- DASHは自動投入しません。Web画面は自動では開きません。
- 過去に失敗したURLを再試行する場合はWeb/CLIのキュー再試行を使ってください。
