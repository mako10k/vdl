# vdl — キュー駆動の動画 DL / 索引デーモン

URL から動画を取得し、**ローカルレジストリ**と**埋め込みベクトル索引**で管理する CLI です。

重い処理はすべて **優先付きジョブキュー** に載せ、**デーモン**がバックグラウンド実行します。

| 優先 | ジョブ | 内容 |
|------|--------|------|
| 100 | `download` | 動画 / m3u8 / HTML からの取得 |
| 40 | `scene_detect` | シーン検出 (ffmpeg) |
| 30 | `clip_optimize` | CLIP で代表キーフレーム選定 |
| 20 | `embed` | ローカル Ollama (`nomic-embed-text`) でテキストベクトル化 |

**ダウンロードが常に最優先**。キューが空いたら索引パイプラインを進めます。

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
# URL をキューへ（すぐ戻る）。デーモン未起動なら自動起動
vdl get 'https://example.com/video.mp4'
vdl get 'https://example.com/master.m3u8' -q best

# キュー監視
vdl queue list
vdl daemon status

# レジストリ / 検索
vdl registry list
vdl registry show 1
vdl registry search '海辺のシーン'
```

デーモンは **ジョブ投入時に自動起動**し、**キューが空のまま一定時間（既定 60 秒）で自動終了**します。

```bash
# アイドル秒数（0=自動終了しない）
export VDL_DAEMON_IDLE_SEC=120

# 自動起動したくないとき
vdl get URL --no-daemon
vdl daemon start   # 手動
```

### CLI マップ

| コマンド | 役割 |
|----------|------|
| `vdl get URL` | **download ジョブをキュー投入**（既定） |
| `vdl get URL --sync` | 同期 DL（デバッグ用） |
| `vdl get URL --no-index` | 索引チェーンを載せない |
| `vdl queue list/show/cancel/retry` | キュー操作 |
| `vdl queue reindex <download_id>` | scene→clip→embed 再投入 |
| `vdl daemon start/stop/status/run` | デーモン |
| `vdl registry list/status/show/search` | レジストリ・ベクトル検索 |

### 同期デバッグ

```bash
vdl get 'https://example.com/a.mp4' --sync --no-index
vdl queue reindex 1
vdl daemon run   # フォアグラウンドで処理
```

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
```

## 非目標（MVP）

- DRM / DASH 完全対応
- マルチワーカー並列デーモン
- CLIP によるテキスト→画像クロスモーダル検索（text 検索が主）

## 開発

```bash
pip install -e ".[dev,index]"
pytest
python -m vdl daemon status
```
