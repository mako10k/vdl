# vdl — キュー駆動の動画 DL / 索引デーモン

URL から動画を取得し、**ローカルレジストリ**と**埋め込みベクトル索引**で管理する CLI です。

重い処理はすべて **優先付きジョブキュー** に載せ、**デーモン**がバックグラウンド実行します。

| 優先 | ジョブ | 内容 |
|------|--------|------|
| 100 | `download` | 動画 / m3u8 / HTML からの取得 |
| 50 | `ts_to_mp4` | レジストリ内の `.ts` を ffmpeg で `.mp4` にリマックス |
| 45 | `dedup` | 同一ファイルを1本に。`.ts` 変換済みは元ハッシュを両方持っているときだけ任意比較（無い／片側は変換後ハッシュ） |
| 40 | `scene_detect` | シーン検出 (ffmpeg) |
| 30 | `clip_optimize` | CLIP で代表キーフレーム選定 |
| 20 | `embed` | ローカル Ollama (`nomic-embed-text`) でテキストベクトル化 |

**ダウンロードが常に最優先**。キューが空いたら `.ts` 変換・重複除去、そのあと索引パイプラインを進めます。

API サーバ起動時に `ts_to_mp4`（レジストリ全件スイープ）と `dedup`（変換後）を自動投入します。手動は `vdl queue maintain` または `POST /api/queue/maintain`。

## 設計・計画

| 文書 | パス |
|------|------|
| 初期アーキテクチャ | [`docs/design/vdl-architecture.dsl`](docs/design/vdl-architecture.dsl) |
| キュー / 索引設計 | [`docs/design/vdl-queue-index.dsl`](docs/design/vdl-queue-index.dsl) |
| 計画 | [`plans/vdl-queue-index.pert`](plans/vdl-queue-index.pert) |

## 必要環境

- Python 3.10+
- `ffmpeg` / `ffprobe`
- 索引用: `torch` + `open-clip-torch`、[Ollama](https://ollama.com)（`nomic-embed-text` 推奨）

```bash
pip install -e ".[dev,index]"
ollama pull nomic-embed-text
```

## 使い方

### 典型フロー

```bash
# 動画 / m3u8 → ダウンロードジョブ投入（すぐ戻る）
vdl get 'https://example.com/video.mp4'
vdl get 'https://example.com/master.m3u8' -q best

# HTML ページ → 既定は候補一覧のみ（ジョブは作らない）
vdl get 'https://example.com/watch/123'
vdl candidates 'https://example.com/watch/123'

# 候補をキュー投入（オプトイン）
vdl get 'https://example.com/watch/123' --enqueue -y    # 新規すべて
vdl candidates 'https://...' -e                         # 対話で番号選択して投入

# キュー監視（progress / ETA 付き）
vdl queue list              # 古い done は既定で非表示（直近10分＋種別最終のみ）
vdl queue list --all-done   # done 履歴も表示
vdl queue list -n 20 --done-within 30
vdl queue show 1
vdl watch                   # ライブ更新（Ctrl+C で終了）
vdl watch --active -n 0.5
vdl watch --until-idle      # キュー空になったら終了
vdl registry list      # index 進捗 % も表示
vdl daemon status

# レジストリ
vdl registry list
vdl registry show 1

# 検索
# 既定: CLIP 視覚検索（日本語は Ollama で英訳してから検索）
vdl search text '海辺のシーン'
vdl search text 'beach' --modality clip
vdl search text 'index.mp4' --modality text   # ファイル名メタ寄り
vdl search similar 2                   # 見た目が似ている動画（CLIP）
vdl search similar 2 --modality text   # テキスト埋め込みでの類似
vdl search similar 2 --scene 3         # シーン 3 を基準
vdl registry search '夕焼け'           # alias
vdl registry similar 2                 # alias
```

デーモンは **ジョブ投入時に自動起動**し、**キューが空のまま一定時間（既定 60 秒）で自動終了**します。  
実行可能なジョブは **最大 3 並列**（`VDL_DAEMON_WORKERS`）で処理します。優先度の高い download から claim されます。

### スーパーバイザ

`vdl get` は既定で **supervisor** を自動起動します（`VDL_SUPERVISOR=0` でワーカー直起動）。

| 役割 | プロセス |
|------|----------|
| supervisor | キューに仕事があれば worker を起動・クラッシュ時に再起動。空きが続けばアイドル終了 |
| worker (`daemon`) | 実際のジョブ実行（並列 / reload） |

```bash
vdl supervisor status
vdl supervisor start
vdl supervisor stop --with-worker
export VDL_SUPERVISOR_IDLE_SEC=120
```

### 自己更新（`daemon_reload`）

ジョブの切れ目（in-flight が 0 のとき）にパッケージコードの変更を検知すると、  
キューに **`daemon_reload`（self-update gate）** が入ります。

- 他の pending ルートジョブは **`depends_on = reload_job_id`** が付き、一覧上も依存が見える
- ゲート実行中は並列度が落ち、reload のみ実行 → デーモンを **re-exec** して新コードを読み込み
- 無効化: `export VDL_DAEMON_AUTO_RELOAD=0`

```bash
# アイドル秒数（0=自動終了しない）
export VDL_DAEMON_IDLE_SEC=120
# 並列ワーカー数
export VDL_DAEMON_WORKERS=3
# ジョブ失敗時の最大試行回数（既定 3 = 初回+自動リトライ2回）。超過後は依存後続を自動キャンセル
export VDL_JOB_MAX_ATTEMPTS=3
# 終了ジョブ (done/failed/cancelled) をアーカイブするまでの分数（既定 60）
export VDL_JOB_ARCHIVE_MINUTES=60
# 一覧に残す failed / cancelled の分数
export VDL_LIST_FAILED_MINUTES=30
export VDL_LIST_CANCELLED_MINUTES=10
# アーカイブ前にリトライ可能な失敗を自動リトライ（クールダウン分）
export VDL_JOB_HOUSEKEEP_RETRY_AFTER_MIN=5
export VDL_JOB_HOUSEKEEP_AUTO_RETRY=1
# 一覧の done 保持分・表示件数
export VDL_LIST_DONE_MINUTES=10
export VDL_LIST_LIMIT=30

# 自動起動したくないとき
vdl get URL --no-daemon
vdl daemon start   # 手動
vdl daemon run --workers 3 --idle 60
```

### CLI マップ

| コマンド | 役割 |
|----------|------|
| `vdl get URL` | 動画/m3u8 → DL ジョブ投入。HTML → **候補一覧のみ** |
| `vdl get URL --enqueue` / `-e` | HTML 候補を DL ジョブ投入（オプトイン） |
| `vdl candidates URL` | HTML スクレイプ専用（同上） |
| `vdl get URL --sync` | 同期 DL（デバッグ用） |
| `vdl get URL --no-index` | 索引チェーンを載せない |
| `vdl queue list/show/cancel/retry` | キュー操作 |
| `vdl queue reindex <download_id>` | scene→clip→embed 再投入（API は `POST /api/registry/{id}/reindex`） |
| `vdl queue maintain` | `.ts`→`.mp4` + 重複除去を投入（API 起動時と同じ。`POST /api/queue/maintain`） |
| `vdl watch` | キュー進捗のライブ表示 |
| `vdl supervisor start/stop/status/run` | **スーパーバイザ**（ワーカー監視・再起動） |
| `vdl daemon start/stop/status/run` | ワーカー（**最大3並列**） |
| `vdl registry list/status/show/search/similar` | レジストリ |
| `vdl search text "…"` | テキスト類似検索 |
| `vdl search similar <download_id>` | レジストリ指定の類似検索（既定 CLIP） |

### 同期デバッグ

```bash
vdl get 'https://example.com/a.mp4' --sync --no-index
vdl queue reindex 1
vdl daemon run   # フォアグラウンドで処理
```

### Web UI / Chrome 拡張

| 面 | 内容 |
|----|------|
| API | `python -m uvicorn api.main:app --host 127.0.0.1 --port 18765` |
| Web console | `cd web && npm run dev` → `http://127.0.0.1:18766` |
| Chrome 拡張 | [`extension/`](extension/) — ページ内動画検出 → DL 投入 / Console 連携（API+UI 起動前提） |

拡張: 検出結果を `POST /api/bridge/push` し、Console は `?vdl_bridge=TOKEN&tab=candidates` で取り込み。手順は [`extension/README.md`](extension/README.md)。

## データ配置 (`~/.vdl` または `$VDL_HOME`)

| パス | 内容 |
|------|------|
| `downloads/` | 取得ファイル |
| `registry.db` | downloads + **jobs** + **scenes** + **embeddings** メタ |
| `chroma/` | ベクトル本体（clip / text は別コレクション） |
| `keyframes/` | CLIP 選定キーフレーム |
| `daemon.pid` / `daemon.log` | デーモン |

## 索引パイプライン

1. **シーン検出** — ffmpeg `scene` フィルタ（失敗時は固定間隔）
2. **CLIP 最適化** — シーン内候補フレームを open_clip で埋め込み、centroid 最近傍を代表に
3. **ローカル LLM ベクトル化** — Ollama embeddings（既定 `nomic-embed-text`）でシーン文書を embed

環境変数例:

```bash
export VDL_OLLAMA_EMBED_MODEL=nomic-embed-text
export VDL_CLIP_MODEL=ViT-B-32
export VDL_SCENE_THRESHOLD=0.3
# 検索: 日本語→英語（CLIP 用）と結果のドメイン多様性
export VDL_OLLAMA_CHAT_MODEL=gemma4   # インストール済みチャットモデル
export VDL_SEARCH_TRANSLATE=1
export VDL_SEARCH_MAX_PER_DOMAIN=2
```

## 非目標（MVP）

- DRM / DASH 完全対応
- 複数ホストにまたがる分散ワーカー（単一プロセス内の `VDL_DAEMON_WORKERS` 並列は対応）

## 開発

```bash
pip install -e ".[dev,index]"
pytest
python -m vdl daemon status
```

## Web UI (Vite + FastAPI)

既定ポート（使用中の 5173 等を避ける）:

| 役割 | ポート | 環境変数 |
|------|--------|----------|
| API | **18765** | `VDL_API_PORT` |
| UI | **18766** | `VDL_WEB_PORT` |

```bash
pip install -e ".[web]"
cd web && npm install
# API と Frontend を同時に起動
npm run dev:all
```

- UI: http://127.0.0.1:18766  
- API docs: http://127.0.0.1:18765/docs  

`npm run dev:all` は起動した API と UI を一緒に停止します。API がすでに動いていれば再利用し、停止しません。個別起動も可能ですが、UI の API 未接続表示が出た場合は、`VDL_API_PORT` が両者で一致していることを確認してください。

詳細は [`web/README.md`](web/README.md)。
