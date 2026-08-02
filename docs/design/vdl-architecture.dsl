framework VdlArchitectureDesign:
  requires problem and decision
  requires premise and evidence
  warns pending

domain VideoDownloadCli:
  description |
    URL を入力すると動画ファイル・HLS インデックス・HTML ページを判別し、
    ローカルダウンロードレジストリで重複を防ぎながら動画を取得する CLI（vdl）の設計。

# ---------------------------------------------------------------------------
# Problems
# ---------------------------------------------------------------------------

problem P1:
  "入力 URL をどう分類し、種別ごとにどの処理パイプラインへ分岐させるか"
  annotation rationale:
    "ユーザー要求の 1/2/3 は URL 種別に対応する。分類が設計の入口である"

problem P2:
  "ダウンロード履歴と重複防止を担うローカルレジストリをどう設計するか"
  annotation rationale:
    "m3u8 は URL が異なっても同一ストリームになり得るため、単純な URL キーだけでは不十分"

problem P3:
  "HTML ページから動画候補を抽出し、既ダウンロードと新規をどう提示・選択させるか"

problem P4:
  "HLS (.m3u8) の取得・品質選択・結合をどの責務分割で実装するか"

problem P5:
  "CLI のコマンド面・設定面・永続データ配置をどう固定するか"

problem P6:
  "既存プロトタイプ実装と本設計の関係をどう扱うか"
  annotation caveat:
    "src/vdl にプロトタイプが既にある。設計を正とし、計画段階でギャップを埋める"

# ---------------------------------------------------------------------------
# Premises (shared assumptions)
# ---------------------------------------------------------------------------

step S1:
  premise PR1:
    "第一級の入力は単一の URL（またはローカルのインデックスパス）であり、対話は HTML 候補選択時に限定する"

step S2:
  premise PR2:
    "永続データはユーザーホーム配下（既定 ~/.vdl）に置き、プロジェクト cwd に依存しない"

step S3:
  premise PR3:
    "HLS の実体結合には外部の ffmpeg を利用してよい（再発明しない）"

step S4:
  premise PR4:
    "対象は一般的な非 DRM 配信（平文 HLS / 直リンク動画）に限定し、Widevine 等は対象外"

step S5:
  premise PR5:
    "重複防止の正はローカル SQLite レジストリであり、ファイルシステム上のファイル有無だけに依存しない"

# ---------------------------------------------------------------------------
# Evidence (requirements & constraints)
# ---------------------------------------------------------------------------

step S6:
  evidence EV1:
    "ユーザー要求: URL が動画ファイルならダウンロードフォルダへ直接保存する"

step S7:
  evidence EV2:
    "ユーザー要求: .m3u8 等インデックスはローカル DB を参照し、ダウンロードレジストリで重複を防ぐ"

step S8:
  evidence EV3:
    |
      ユーザー要求: .html 等はスクレイプして候補抽出し、
      既存ダウンロード済ならその旨表示、新規は選択制でダウンロードする

step S9:
  evidence EV4:
    "Content-Type・拡張子・ボディ先頭（#EXTM3U / HTML）はいずれも不完全なため、複数シグナルの合成が必要"

step S10:
  evidence EV5:
    "マスター m3u8 は複数バリアントを持ち、同一論理ストリームでもメディアプレイリスト URL が品質ごとに異なる"

step S11:
  evidence EV6:
    "HTML 内のメディア URL は video/source/a/meta/script/json-ld/iframe など複数箇所に現れる"

step S12:
  evidence EV7:
    "同一バイト列でも別 URL になり得るため、完了後の content hash も二次キーとして有用"

step S13:
  evidence EV8:
    "レジストリ照合で status=failed を completed と同等に扱うと再試行不能になる（OR/AND 優先順位にも注意）"

# ---------------------------------------------------------------------------
# MECE: URL resource kinds
# ---------------------------------------------------------------------------

step S14:
  viewpoint VP1:
    axis resource_kind

step S15:
  partition PT1 on VideoDownloadCli axis resource_kind:
    Video := direct_video_bytes
    Index := playlist_index_m3u8_or_mpd
    Html := navigable_document
    Unknown := not Video and not Index and not Html

step S16:
  decision D1 based_on P1, PR1, EV1, EV2, EV3, EV4:
    |
      URL 分類の結果を ResourceKind = {video, index, html, unknown} の排他集合とする。
      unknown は html 取得を試し、失敗時はエラー終了する。
      分類優先順位は content-type（曖昧でない場合）> body sniff > URL 拡張子 > パス無しは html とみなす。

# ---------------------------------------------------------------------------
# Pipeline decisions per kind
# ---------------------------------------------------------------------------

step S17:
  decision D2 based_on P1, EV1, PR5:
    |
      video: レジストリで canonical URL の completed を照会し、
      未登録ならストリーム DL → ハッシュ計算 → registry 完了記録。
      --force 時のみ再 DL。

step S18:
  decision D3 based_on P1, P4, EV2, EV5, PR3, PR4:
    |
      index (m3u8): プレイリスト取得 → master なら quality でバリアント選択 →
      media playlist の fingerprint を計算 → URL または fingerprint で completed 照会 →
      未登録なら ffmpeg で結合 DL → registry 完了記録（fingerprint 必須保存）。
      MPD (DASH) は将来拡張とし、MVP では未対応を明示エラーとする。

step S19:
  decision D4 based_on P1, P3, EV3, EV6:
    |
      html: ページ取得 → 候補抽出 → 各候補をレジストリ照合して 済/新規 を表形式表示 →
      対話選択（または -y / --all）→ 選択 URL を video/index パイプラインへ再入する。
      候補ゼロは失敗ではなく「候補なし」終了とする。

# ---------------------------------------------------------------------------
# Registry design
# ---------------------------------------------------------------------------

step S20:
  viewpoint VP2:
    axis registry_identity

step S21:
  partition PT2 on VideoDownloadCli axis registry_identity:
    ByUrl := canonical_url_match
    ByFingerprint := m3u8_media_fingerprint
    ByContentHash := completed_file_sha256
    Others := not ByUrl and not ByFingerprint and not ByContentHash

step S22:
  decision D5 based_on P2, PR2, PR5, EV2, EV5, EV7, EV8:
    |
      レジストリは SQLite 単一ファイル（~/.vdl/registry.db）。
      downloads テーブルに url, canonical_url, source_type, status,
      file_path, file_size, file_hash, m3u8_fingerprint, parent_page_url,
      title, content_type, meta_json, error, created_at, updated_at を持つ。
      status ∈ {pending, completed, failed, skipped}。
      重複判定の正は status=completed のみ。failed は再試行可。
      照会条件は (url OR canonical_url) AND status=completed と括弧必須。

step S23:
  decision D6 based_on P2, EV5, EV7:
    |
      m3u8 fingerprint は media playlist のセグメント URI 列（VOD）または
      media_sequence+first/last（ライブ窓）から SHA-256 する。
      is_downloaded(url, fingerprint?) は fingerprint 一致を URL 一致より優先して返す。
      content hash は完了後に記録し、将来の二次 dedupe / 整合確認に使う（MVP の主キーにはしない）。

step S24:
  decision D7 based_on P2, PR2:
    |
      保存先は ~/.vdl/downloads、作業領域は ~/.vdl/tmp。
      VDL_HOME または CLI --home でルートを差し替え可能とする。

# ---------------------------------------------------------------------------
# HTML scraping & selection
# ---------------------------------------------------------------------------

step S25:
  decision D8 based_on P3, EV3, EV6:
    |
      候補抽出ソースは MECE に近い優先集合として:
      video/source タグ、a[href] メディア拡張子、og:video 系 meta、
      JSON-LD、script 内 URL、iframe src（メディア拡張子のみ）。
      URL は絶対化し normalize して一意化。kind は video | index。

step S26:
  decision D9 based_on P3, EV3, PR1:
    |
      対話選択は番号・範囲・new・all・キャンセルを受理する。
      -y は新規のみ自動 DL、--all は全候補対象（済は force なしでスキップ）。
      既ダウンロード表示は registry id と path を必ず出す。

# ---------------------------------------------------------------------------
# HLS quality & download
# ---------------------------------------------------------------------------

step S27:
  decision D10 based_on P4, EV5, PR3:
    |
      quality は best | worst | 0-based index | 解像度部分一致（720/1080 等）。
      master 表示時はバリアント一覧をログし、選択結果の media URL を明示する。
      実 DL は ffmpeg -c copy を第一選択とし、aac_adtstoasc 失敗時は bsf なしで再試行。

step S28:
  pending PD1:
    "暗号化 HLS（AES-KEY）や live 窓の追従録画は MVP 対象外。需要があれば後続 problem とする"

# ---------------------------------------------------------------------------
# CLI surface
# ---------------------------------------------------------------------------

step S29:
  decision D11 based_on P5, PR1, PR2:
    |
      コマンド面:
      get URL — 主入口（分類〜DL）
      list — レジストリ一覧
      status URL — 登録確認
      info — パス表示
      open-dir — ダウンロードディレクトリパス出力
      グローバル --home。get の主要オプションは -q/--quality, -f/--force, -H/--header, -y, --all, --timeout。

step S30:
  decision D12 based_on P5:
    |
      パッケージ名/CLI 名は vdl。Python 3.10+、依存は httpx, beautifulsoup4, lxml, m3u8, rich, click。
      エントリは vdl と python -m vdl。

# ---------------------------------------------------------------------------
# Module architecture
# ---------------------------------------------------------------------------

step S31:
  viewpoint VP3:
    axis module_layer

step S32:
  partition PT3 on VideoDownloadCli axis module_layer:
    Cli := user_interface_click
    Service := orchestration_and_ux
    DomainAdapters := classify_download_m3u8_scrape
    Registry := persistence
    Config := paths_and_env

step S33:
  decision D13 based_on P1, P2, P3, P4, P5:
    |
      モジュール責務:
      cli.py — 引数と終了コード
      service.py — 分類結果に応じたパイプラインと HTML 選択 UX
      classifier.py — ResourceKind 判定
      downloader.py — 直ファイルストリーム DL
      m3u8_dl.py — 解析・fingerprint・ffmpeg
      scraper.py — HTML 候補抽出
      registry.py — SQLite 重複防止
      config.py / util.py — パス・正規化・ハッシュ
      service 以外から CLI を呼ばない。registry は service と list/status からのみ触る。

# ---------------------------------------------------------------------------
# Relation to existing prototype
# ---------------------------------------------------------------------------

step S34:
  evidence EV9:
    "src/vdl に上記モジュール分割に近いプロトタイプが既に存在し、基本フローは動作確認済み"

step S35:
  decision D14 based_on P6, EV9:
    |
      本設計を正とする。プロトタイプは設計の実装ドラフトとみなし、
      計画では設計ギャップ（MPD 明示エラー、ライブ方針、テスト整備、ドキュメント同期、
      レジストリ SQL 安全性の回帰テスト等）をタスク化する。
      大規模リライトはせず、設計準拠への収束を優先する。

# ---------------------------------------------------------------------------
# Non-goals
# ---------------------------------------------------------------------------

step S36:
  decision D15 based_on PR4, PD1:
    |
      非目標（MVP）: DRM、ブラウザ自動化ログイン、GUI、並列マルチ URL バッチ CLI、
      字幕/音声トラックの高度選択、DASH/MPD 完全対応、リモート共有レジストリ。

# ---------------------------------------------------------------------------
# Scope notes: D2/D3/D4 は P1 上の排他分岐であり競合ではない
# ---------------------------------------------------------------------------

step S37:
  comparison CMP1 on P1 viewpoint VP1 relation incomparable D2, D3:
    "video パイプラインと index パイプラインは resource_kind で排他であり競合しない"

step S38:
  comparison CMP2 on P1 viewpoint VP1 relation incomparable D3, D4:
    "index パイプラインと html パイプラインは resource_kind で排他であり競合しない"

step S39:
  comparison CMP3 on P1 viewpoint VP1 relation incomparable D2, D4:
    "video パイプラインと html パイプラインは resource_kind で排他であり競合しない"

# ---------------------------------------------------------------------------
# Queries for review
# ---------------------------------------------------------------------------

query Q1:
  .problems[] | select(.id == "P1") | related_decisions

query Q2:
  .problems[] | select(.id == "P2") | related_decisions

query Q3:
  .problems[] | select(.id == "P3") | related_decisions

query Q4:
  .problems[] | select(.id == "P4") | related_decisions

query Q5:
  .problems[] | select(.id == "P5") | related_decisions

query Q6:
  .problems[] | select(.id == "P6") | related_decisions

query Q7:
  .steps[] | select(.role == "decision")

query Q8:
  .steps[] | select(.role == "pending")
