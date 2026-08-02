# vdl — 動画ダウンロード CLI（レジストリ付き）

URL を渡すと種類を判定し、**動画ファイル / HLS (`.m3u8`) / HTML ページ**を適切に処理します。  
ダウンロード履歴はローカル SQLite レジストリに保存し、**重複ダウンロードを防ぎます**。

設計（llmthink）: [`docs/design/vdl-architecture.dsl`](docs/design/vdl-architecture.dsl)  
実装計画（perttool）: [`plans/vdl-mvp.pert`](plans/vdl-mvp.pert)  
ギャップ監査: [`docs/design/gap-audit.md`](docs/design/gap-audit.md)

## 動作

| URL の種類 | 処理 |
|-----------|------|
| **動画ファイル** (`.mp4` 等 / `Content-Type: video/*`) | `~/.vdl/downloads` へ直接保存。URL がレジストリにあればスキップ |
| **インデックス** (`.m3u8`) | プレイリスト解析 → fingerprint で重複判定 → `ffmpeg` で結合 |
| **HTML** | スクレイプして候補抽出。済/新規を表示し、番号選択でダウンロード |
| **ローカルパス** | 動画ファイルのコピー登録、またはローカル `.m3u8` の解析・取得 |

## 必要環境

- Python 3.10+
- `ffmpeg`（m3u8 / HLS 用）

## インストール

```bash
cd /path/to/video
pip install -e .
# 開発・テスト
pip install -e ".[dev]"
pytest
```

## 使い方

```bash
# 動画 URL をそのままダウンロード
vdl get 'https://example.com/video.mp4'

# m3u8（品質: best | worst | 0 | 720 | 1080 など）
vdl get 'https://example.com/master.m3u8' -q best
vdl get 'https://example.com/master.m3u8' -q 720

# ローカルの m3u8
vdl get ./playlist.m3u8

# HTML ページから候補を出して対話選択
vdl get 'https://example.com/watch/123'

# 新規候補をすべて自動 DL（対話なし）
vdl get 'https://example.com/watch/123' -y

# ヘッダ付与（Referer / Cookie など）
vdl get 'https://example.com/stream.m3u8' \
  -H 'Referer: https://example.com/' \
  -H 'Cookie: session=...'

# 強制再ダウンロード
vdl get 'https://example.com/video.mp4' --force

# 履歴
vdl list
vdl list --status completed
vdl status 'https://example.com/video.mp4'

# パス確認
vdl info
```

### HTML 候補の選択入力

```
1 3        # 1番と3番
1-4        # 1〜4
new        # 新規のみ
all        # すべて（済は force なしでスキップ）
(Enter)    # キャンセル
```

## データ配置

| パス | 内容 |
|------|------|
| `~/.vdl/downloads/` | 保存先 |
| `~/.vdl/registry.db` | ダウンロードレジストリ (SQLite) |
| `~/.vdl/tmp/` | 作業用 |

環境変数 `VDL_HOME` または `vdl --home /path get ...` で変更可能です。

## 重複判定

- **動画 URL**: 正規化した URL が `status=completed` ならスキップ
- **m3u8**: プレイリスト URL に加え、セグメント一覧から作った **fingerprint** でも照合
- `failed` は再試行可能（重複扱いしない）

## 非目標（MVP）

- DRM / Widevine 等
- MPEG-DASH (`.mpd`) 完全対応（検出時は明示エラー）
- 暗号化 HLS の保証対応（警告のみ。鍵取得可能な場合のみ成功しうる）
- ライブ窓の追従録画
- ブラウザ自動化ログイン / GUI
- リモート共有レジストリ

## 開発

```bash
pip install -e ".[dev]"
pytest
python -m vdl get 'https://...'
```
