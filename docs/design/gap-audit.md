# GAP_AUDIT — 設計 D1–D15 vs `src/vdl` 実装

日時: 2026-08-02  
対象: thought `vdl-architecture` (finalized) / `src/vdl/*`

| ID | 設計要点 | 実装 | 判定 | 残ギャップ / 対応タスク |
|----|----------|------|------|------------------------|
| D1 | ResourceKind 排他 + 分類優先順位 | `classifier.py` | **OK** | 回帰テスト不足 → CLASSIFY_IMPL |
| D2 | video: 照会→DL→hash→完了 | `service._handle_video` | **OK** | 回帰テスト → VIDEO_IMPL / TEST |
| D3 | m3u8 fingerprint + ffmpeg; **MPD は明示エラー** | `m3u8_dl` / service | **GAP** | `.mpd` を index 扱いのまま。未対応メッセージ無し → M3U8_IMPL |
| D4 | html 候補→済/新規→選択→再入 | `service._handle_html` | **OK** | e2e テスト不足 → HTML / TEST |
| D5 | SQLite スキーマ・completed のみ | `registry.py` | **OK** | SQL 括弧修正済。ユニットテスト → REGISTRY_IMPL |
| D6 | fingerprint 優先 + content hash 二次 | registry / m3u8 | **OK** | hash は記録のみ（設計どおり主キーにしない） |
| D7 | `~/.vdl` / VDL_HOME / --home | `config.py` / cli | **OK** | — |
| D8 | 複数ソース候補抽出 | `scraper.py` | **OK** | mpd 候補を index として出す → 選択後 M3U8 で弾く or 抽出時注記 |
| D9 | 選択 UI (番号/範囲/new/all) | service | **OK** | — |
| D10 | quality best/worst/index/解像度 | m3u8_dl | **OK** | — |
| D11 | get/list/status/info/open-dir + 主要オプション | cli | **OK** | — |
| D12 | vdl / Python 3.10+ / 依存 | pyproject | **OK** | — |
| D13 | モジュール責務分割 | パッケージ構成 | **OK** | — |
| D14 | プロトタイプを設計に収束 | 本監査 | **進行中** | 以下ギャップを実装タスクで埋める |
| D15 | 非目標 (DRM/GUI/…) | コードに未実装 | **OK** | ドキュメントに非目標明記 → DOCS |
| PD1 | 暗号化 HLS / live 追従は対象外 | 暗黙 | **GAP** | 検出時のメッセージ強化 → M3U8_IMPL |
| PR1 | ローカルインデックスパスも可 | HTTP のみ | **GAP** | `file://` / ローカル .m3u8 読込 → M3U8_IMPL 寄り |
| 横断 | 自動テスト | なし | **GAP** | `tests/` 整備 → REGISTRY/CLASSIFY/TEST |
| 横断 | README ↔ 設計リンク | 弱い | **GAP** | DOCS_IMPL |

## 優先対応（計画タスクへ割当）

1. **REGISTRY_IMPL** — ユニットテスト + 回帰（failed 非重複、括弧付き WHERE）
2. **CLASSIFY_IMPL** — 分類マトリクステスト + 微調整
3. **VIDEO_IMPL** — 直 DL 経路の回帰確認・必要なら小修正
4. **M3U8_IMPL** — MPD 明示エラー、EXT-X-KEY 警告、ローカル m3u8
5. **HTML_IMPL** — mpd 候補の扱い整理、表示強化
6. **CLI_IMPL** — 仕上げ確認
7. **TEST_IMPL** — 統合テスト
8. **DOCS_IMPL** — README + 非目標
9. **E2E_VERIFY** — 受入

## 結論

コアフローは設計と概ね一致。ブロッカー級ギャップは:

- MPD 未対応の明示不足
- 自動テスト欠如
- ローカル path / 暗号化 HLS の境界メッセージ

大規模リライト不要。硬化 + テスト + 境界エラーで MVP 完了可能。
