framework VdlQueueIndexDesign:
  requires problem and decision
  requires premise and evidence
  warns pending

domain VideoDownloadCliQueue:
  description |
    vdl を「同期ダウンロード CLI」から
    「優先付きジョブキュー + バックグラウンドデーモン + 埋め込みベクトル索引」へ拡張する設計。

problem P1:
  "ダウンロードとインデックス化をどう非同期ジョブとして分離し、優先順位を固定するか"

problem P2:
  "シーン検出 → CLIP 最適化 → ローカル LLM ベクトル化の索引パイプラインをどう定義するか"

problem P3:
  "埋め込みベクトルをレジストリとどう統合し、検索可能にするか"

problem P4:
  "CLI の役割をキュー操作・デーモン操作・レジストリ操作へどう再編するか"

step S1:
  premise PR1:
    "ユーザー操作は待ち時間を嫌い、重い索引処理はバックグラウンドでよい"

step S2:
  premise PR2:
    "ダウンロード完了前に索引は開始できない（ファイル実体が必要）"

step S3:
  premise PR3:
    "同一マシン上のローカル推論（CLIP / Ollama）を使い、クラウド API に依存しない"

step S4:
  premise PR4:
    "既存 downloads レジストリは維持し、ジョブとベクトルは隣接テーブル/ストアで拡張する"

step S5:
  evidence EV1:
    "要求: 埋め込みベクトルインデックスをレジストリに付ける"

step S6:
  evidence EV2:
    "要求パイプライン: シーン検出 → CLIP 最適化 → ローカル LLaMA 系でベクトル化"

step S7:
  evidence EV3:
    "要求: ダウンロードも含めキュー形式でデーモンが実行し、ダウンロード優先"

step S8:
  evidence EV4:
    "要求: ローカル CLI はキュー状態管理・操作とレジストリ操作が中心"

step S9:
  evidence EV5:
    "環境: open_clip + torch、Ollama（nomic-embed-text 等）、ffmpeg が利用可能"

# --- Queue ---

step S10:
  viewpoint VP1:
    axis job_kind

step S11:
  partition PT1 on VideoDownloadCliQueue axis job_kind:
    Download := download_fetch_and_register
    SceneDetect := scene_boundary_extraction
    ClipOptimize := clip_keyframe_selection
    Embed := llama_or_ollama_vectorization
    Others := not Download and not SceneDetect and not ClipOptimize and not Embed

step S12:
  decision D1 based_on P1, PR1, PR2, EV3:
    |
      全作業を jobs キューに載せる。
      kind ∈ {download, scene_detect, clip_optimize, embed}。
      優先度は download=100、scene_detect=40、clip_optimize=30、embed=20。
      デーモンは常に runnable かつ priority 最大の 1 ジョブを実行する（単一ワーカー MVP）。
      索引ジョブは parent download の status=completed と依存ジョブ完了を前提にする。

step S13:
  decision D2 based_on P1, PR2, EV3:
    |
      download 完了時に scene_detect を自動エンキュー（--no-index で抑制可）。
      scene_detect 完了 → clip_optimize、clip_optimize 完了 → embed をチェーンする。
      依存は jobs.depends_on_job_id で表現する。

# --- Index pipeline ---

step S14:
  decision D3 based_on P2, EV2, EV5, PR3:
    |
      シーン検出: ffmpeg scene フィルタ（閾値既定 0.3）で境界を抽出し scenes テーブルへ保存。
      フォールバック: 固定間隔キーフレーム（例 5s）。

step S15:
  decision D4 based_on P2, EV2, EV5:
    |
      CLIP 最適化: 各シーンから候補フレームを数枚抽出し、open_clip で埋め込み。
      シーン代表は「シーン内平均 CLIP ベクトルに最も近いフレーム」を選ぶ（centroid 近傍）。
      代表フレームの CLIP ベクトルも保存し、視覚類似検索に使う。

step S16:
  decision D5 based_on P2, EV2, EV5, PR3:
    |
      ローカル LLM ベクトル化: Ollama embeddings API を使う。
      既定モデルは nomic-embed-text（Llama 系ローカル埋め込み相当のローカル実行）。
      入力テキストは title + シーン時刻 + 任意キャプション（キャプション生成は optional、既定オフ）。
      テキスト埋め込みを semantic 検索の主キー、CLIP を visual 副キーとする。

step S17:
  pending PD1:
    "シーンキャプションを gemma/llama 生成で付けるかは品質とコストを見て後続で判断する"

# --- Vector store ---

step S18:
  decision D6 based_on P3, PR4, EV1:
    |
      SQLite registry.db に scenes / embeddings メタを持ち、
      実ベクトルは Chroma 永続ディレクトリ ~/.vdl/chroma に格納する。
      embedding 行は download_id, scene_id, modality(clip|text), model, chroma_id, dim を記録。
      レジストリ検索 vdl registry search は text クエリを Ollama で embed し Chroma 近傍検索する。

# --- Daemon ---

step S19:
  decision D7 based_on P1, EV3:
    |
      デーモンは vdl daemon run（フォアグラウンド）と vdl daemon start/stop/status。
      PID ファイル ~/.vdl/daemon.pid、ログ ~/.vdl/daemon.log。
      ポーリング間隔既定 1s。停止は graceful（現在ジョブ完了待ち or タイムアウト）。

# --- CLI surface ---

step S20:
  decision D8 based_on P4, EV4:
    |
      CLI 再編:
      get/enqueue — ダウンロードジョブ投入（既定）
      queue list|show|cancel|retry|drain
      daemon start|stop|status|run
      registry list|status|show|search|reindex
      同期実行は get --sync に限定（デバッグ用）。

step S21:
  decision D9 based_on P4, PR1:
    |
      通常 UX: vdl get URL はキューに download を載せ、job id を表示してすぐ戻る。
      デーモン未起動時は警告し、--sync を案内する（自動起動はしない）。

step S22:
  decision D10 based_on P1, P2, P3:
    |
      モジュール:
      queue.py — jobs CRUD / claim / complete
      daemon.py — worker loop
      index/scene.py, index/clip_opt.py, index/embed.py
      vectorstore.py — Chroma wrapper
      cli に queue/daemon/registry グループ

query Q1:
  .problems[] | select(.id == "P1") | related_decisions

query Q2:
  .problems[] | select(.id == "P2") | related_decisions

query Q3:
  .problems[] | select(.id == "P3") | related_decisions

query Q4:
  .problems[] | select(.id == "P4") | related_decisions
