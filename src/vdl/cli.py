"""vdl CLI — queue / daemon / registry + enqueue downloads."""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Optional

import click
from rich.console import Console
from rich.table import Table

from vdl import __version__
from vdl.config import Config
from vdl.daemon import (
    daemon_status,
    ensure_daemon_running,
    run_forever,
    start_background,
    stop_daemon,
)
from vdl.queue import JobQueue
from vdl.registry import Registry
from vdl.util import normalize_url
from vdl.workers import enqueue_download, enqueue_index_chain

console = Console()


def _parse_header(values: tuple[str, ...]) -> dict[str, str]:
    headers: dict[str, str] = {}
    for v in values:
        if ":" not in v:
            raise click.BadParameter(f"ヘッダは 'Name: Value' 形式: {v}")
        name, val = v.split(":", 1)
        headers[name.strip()] = val.strip()
    return headers


@click.group(context_settings={"help_option_names": ["-h", "--help"]})
@click.version_option(__version__, prog_name="vdl")
@click.option(
    "--home",
    "home_dir",
    type=click.Path(path_type=Path),
    default=None,
    help="データディレクトリ (default: ~/.vdl または $VDL_HOME)",
)
@click.pass_context
def main(ctx: click.Context, home_dir: Optional[Path]) -> None:
    """vdl — キュー駆動の動画 DL / 索引デーモン + レジストリ CLI"""
    ctx.ensure_object(dict)
    cfg = Config(home=home_dir) if home_dir else Config()
    cfg.ensure_dirs()
    ctx.obj["config"] = cfg


# ---------------------------------------------------------------------------
# get — enqueue (default) or sync
# ---------------------------------------------------------------------------


@main.command("get")
@click.argument("url")
@click.option(
    "-q",
    "--quality",
    default="best",
    show_default=True,
    help="m3u8 品質: best | worst | 0-based index | 720 / 1080 など",
)
@click.option("-f", "--force", is_flag=True, help="レジストリ済みでも再ダウンロード")
@click.option(
    "-H",
    "--header",
    multiple=True,
    help="追加 HTTP ヘッダ (例: -H 'Referer: https://example.com/')",
)
@click.option(
    "-y",
    "--yes",
    "auto_yes",
    is_flag=True,
    help="HTML 候補は新規のみ（同期時・ジョブ内）",
)
@click.option(
    "--all",
    "select_all",
    is_flag=True,
    help="HTML 候補をすべて対象",
)
@click.option(
    "--timeout",
    default=60.0,
    show_default=True,
    type=float,
    help="HTTP タイムアウト秒",
)
@click.option(
    "--sync",
    is_flag=True,
    help="キューを使わず同期実行（デバッグ用）",
)
@click.option(
    "--no-index",
    is_flag=True,
    help="DL 後の索引パイプラインをエンキューしない",
)
@click.option(
    "--no-daemon",
    is_flag=True,
    help="ジョブ投入後にデーモンを自動起動しない",
)
@click.pass_context
def get_cmd(
    ctx: click.Context,
    url: str,
    quality: str,
    force: bool,
    header: tuple[str, ...],
    auto_yes: bool,
    select_all: bool,
    timeout: float,
    sync: bool,
    no_index: bool,
    no_daemon: bool,
) -> None:
    """URL をダウンロードキューに投入（既定）。必要ならデーモンを自動起動。"""
    config: Config = ctx.obj["config"]
    headers = _parse_header(header)

    if sync:
        from vdl.service import DownloadService

        with DownloadService(
            config,
            quality=quality,
            force=force,
            headers=headers,
            timeout=timeout,
        ) as svc:
            results = svc.process_url(
                url,
                select_all=select_all,
                auto_yes=auto_yes or select_all,
            )
        if not no_index:
            q = JobQueue(config.db_path)
            for r in results:
                if r.record_id and r.status in ("downloaded", "skipped"):
                    enqueue_index_chain(q, r.record_id)
                    console.print(
                        f"[dim]index chain enqueued for download_id={r.record_id}[/dim]"
                    )
            if not no_daemon:
                _auto_start_daemon(config)
        failed = [r for r in results if r.status == "failed"]
        if failed:
            sys.exit(1)
        return

    q = JobQueue(config.db_path)
    job = enqueue_download(
        q,
        url,
        quality=quality,
        force=force,
        headers=headers,
        auto_yes=auto_yes or True,  # non-interactive in worker
        select_all=select_all,
        timeout=timeout,
        no_index=no_index,
    )
    console.print(f"[green]queued[/green] job_id={job.id} kind=download priority={job.priority}")
    console.print(f"  url: {url}")
    if no_daemon:
        st = daemon_status(config)
        if st["running"]:
            console.print(f"[dim]daemon running pid={st['pid']}[/dim]")
        else:
            console.print("[yellow]daemon 未起動[/yellow] (--no-daemon)")
    else:
        _auto_start_daemon(config)


def _auto_start_daemon(config: Config) -> None:
    try:
        pid, started = ensure_daemon_running(config)
    except Exception as e:
        console.print(f"[red]daemon 自動起動失敗[/red] {e}")
        console.print("[dim]`vdl daemon start` を手動で実行してください[/dim]")
        return
    idle = getattr(config, "daemon_idle_sec", 60)
    workers = getattr(config, "daemon_workers", 3)
    if started:
        console.print(
            f"[cyan]daemon auto-started[/cyan] pid={pid} "
            f"workers={workers} (idle exit after {idle:g}s empty queue)"
        )
    else:
        console.print(f"[dim]daemon running pid={pid}[/dim]")


# ---------------------------------------------------------------------------
# queue
# ---------------------------------------------------------------------------


@main.group("queue")
def queue_group() -> None:
    """ジョブキューの状態確認・操作"""


@queue_group.command("list")
@click.option(
    "--status",
    type=click.Choice(
        ["pending", "running", "done", "failed", "cancelled"], case_sensitive=False
    ),
    default=None,
)
@click.option(
    "--kind",
    type=click.Choice(
        ["download", "scene_detect", "clip_optimize", "embed"], case_sensitive=False
    ),
    default=None,
)
@click.option("-n", "--limit", default=30, show_default=True)
@click.pass_context
def queue_list(ctx: click.Context, status: Optional[str], kind: Optional[str], limit: int) -> None:
    config: Config = ctx.obj["config"]
    q = JobQueue(config.db_path)
    rows = q.list_jobs(status=status, kind=kind, limit=limit)
    counts = q.counts()
    console.print(f"[dim]counts={counts}[/dim]")
    if not rows:
        console.print("[dim]ジョブなし[/dim]")
        return
    table = Table(title="jobs")
    table.add_column("ID", justify="right")
    table.add_column("kind")
    table.add_column("pri", justify="right")
    table.add_column("status")
    table.add_column("progress")
    table.add_column("dl_id", justify="right")
    table.add_column("dep", justify="right")
    table.add_column("updated")
    for j in rows:
        style = {
            "pending": "yellow",
            "running": "cyan",
            "done": "green",
            "failed": "red",
            "cancelled": "dim",
        }.get(j.status, "white")
        table.add_row(
            str(j.id),
            j.kind,
            str(j.priority),
            f"[{style}]{j.status}[/{style}]",
            j.progress_label(),
            str(j.download_id or "-"),
            str(j.depends_on_job_id or "-"),
            j.updated_at[:19] if j.updated_at else "-",
        )
    console.print(table)


@queue_group.command("show")
@click.argument("job_id", type=int)
@click.pass_context
def queue_show(ctx: click.Context, job_id: int) -> None:
    config: Config = ctx.obj["config"]
    job = JobQueue(config.db_path).get(job_id)
    if not job:
        console.print(f"[red]job {job_id} not found[/red]")
        sys.exit(1)
    console.print(f"id:       {job.id}")
    console.print(f"kind:     {job.kind}")
    console.print(f"priority: {job.priority}")
    console.print(f"status:   {job.status}")
    console.print(f"progress: {job.progress_label()}")
    if job.progress_msg:
        console.print(f"  msg:    {job.progress_msg}")
    if job.progress_current is not None:
        console.print(
            f"  count:  {job.progress_current}"
            + (f"/{job.progress_total}" if job.progress_total else "")
        )
    if job.eta_sec is not None and job.status == "running":
        from vdl.queue import format_duration

        console.print(f"  eta:    {format_duration(job.eta_sec)}")
    console.print(f"download: {job.download_id}")
    console.print(f"depends:  {job.depends_on_job_id}")
    console.print(f"started:  {job.started_at or '-'}")
    console.print(f"updated:  {job.updated_at}")
    console.print(f"payload:  {job.payload}")
    console.print(f"result:   {job.result}")
    if job.error:
        console.print(f"[red]error:    {job.error}[/red]")


@queue_group.command("cancel")
@click.argument("job_id", type=int)
@click.pass_context
def queue_cancel(ctx: click.Context, job_id: int) -> None:
    config: Config = ctx.obj["config"]
    job = JobQueue(config.db_path).cancel(job_id)
    if not job:
        console.print(f"[red]job {job_id} not found[/red]")
        sys.exit(1)
    console.print(f"cancelled job_id={job.id} status={job.status}")


@queue_group.command("retry")
@click.argument("job_id", type=int)
@click.pass_context
def queue_retry(ctx: click.Context, job_id: int) -> None:
    config: Config = ctx.obj["config"]
    job = JobQueue(config.db_path).retry(job_id)
    if not job:
        console.print(f"[red]job {job_id} not found[/red]")
        sys.exit(1)
    console.print(f"retry job_id={job.id} status={job.status}")


@queue_group.command("reindex")
@click.argument("download_id", type=int)
@click.option("--no-daemon", is_flag=True, help="デーモン自動起動しない")
@click.pass_context
def queue_reindex(ctx: click.Context, download_id: int, no_daemon: bool) -> None:
    """完了済み download に対し scene→clip→embed を再投入"""
    config: Config = ctx.obj["config"]
    reg = Registry(config.db_path)
    rec = reg.get(download_id)
    if not rec or rec.status != "completed":
        console.print(f"[red]download_id={download_id} が completed ではありません[/red]")
        sys.exit(1)
    q = JobQueue(config.db_path)
    s, c, e = enqueue_index_chain(q, download_id)
    console.print(
        f"[green]reindex queued[/green] scene={s.id} clip={c.id} embed={e.id} download_id={download_id}"
    )
    if not no_daemon:
        _auto_start_daemon(config)


# ---------------------------------------------------------------------------
# daemon
# ---------------------------------------------------------------------------


@main.group("daemon")
def daemon_group() -> None:
    """バックグラウンドワーカー（DL 優先）"""


@daemon_group.command("run")
@click.option("--interval", default=1.0, show_default=True, type=float)
@click.option("--pid", "write_pid", is_flag=True, help="PID ファイルを書く")
@click.option(
    "--idle",
    "idle_sec",
    type=float,
    default=None,
    help="キュー空のままこの秒数で自動終了 (0=無効)。未指定時: --pid なら設定値、否則は無効",
)
@click.option(
    "--workers",
    type=int,
    default=None,
    help="並列ジョブ数 (default: VDL_DAEMON_WORKERS または 3)",
)
@click.pass_context
def daemon_run(
    ctx: click.Context,
    interval: float,
    write_pid: bool,
    idle_sec: float | None,
    workers: int | None,
) -> None:
    """フォアグラウンドでワーカーループを実行"""
    config: Config = ctx.obj["config"]
    # Background (--pid): default idle from config. Foreground: no idle unless --idle given.
    if idle_sec is None:
        effective_idle = (
            float(config.daemon_idle_sec) if write_pid else 0.0
        )
    else:
        effective_idle = float(idle_sec)
    run_forever(
        config,
        poll_interval=interval,
        foreground=True,
        write_pid_file=write_pid,
        idle_sec=effective_idle,
        workers=workers,
    )


@daemon_group.command("start")
@click.pass_context
def daemon_start(ctx: click.Context) -> None:
    """バックグラウンドでデーモン起動"""
    config: Config = ctx.obj["config"]
    try:
        pid = start_background(config)
    except RuntimeError as e:
        console.print(f"[yellow]{e}[/yellow]")
        sys.exit(1)
    console.print(f"[green]daemon started[/green] pid={pid}")
    console.print(f"  log: {config.daemon_log_path}")


@daemon_group.command("stop")
@click.pass_context
def daemon_stop(ctx: click.Context) -> None:
    config: Config = ctx.obj["config"]
    ok = stop_daemon(config)
    if ok:
        console.print("[green]daemon stopped[/green]")
    else:
        console.print("[dim]daemon was not running[/dim]")


@daemon_group.command("status")
@click.pass_context
def daemon_status_cmd(ctx: click.Context) -> None:
    config: Config = ctx.obj["config"]
    st = daemon_status(config)
    if st["running"]:
        console.print(f"[green]running[/green] pid={st['pid']}")
    else:
        console.print("[yellow]stopped[/yellow]")
    console.print(f"  pid_file: {st['pid_file']}")
    console.print(f"  log_file: {st['log_file']}")
    console.print(f"  idle_sec: {st.get('idle_sec', config.daemon_idle_sec)} (empty-queue auto-stop)")
    console.print(f"  workers:  {st.get('workers', config.daemon_workers)} (max parallel jobs)")
    console.print(f"  queue:    {st['queue']}")


# ---------------------------------------------------------------------------
# registry
# ---------------------------------------------------------------------------


@main.group("registry")
def registry_group() -> None:
    """ダウンロードレジストリとベクトル索引の操作"""


@registry_group.command("list")
@click.option(
    "--status",
    type=click.Choice(["completed", "failed", "pending", "skipped"], case_sensitive=False),
    default=None,
)
@click.option("-n", "--limit", default=50, show_default=True)
@click.pass_context
def registry_list(ctx: click.Context, status: Optional[str], limit: int) -> None:
    config: Config = ctx.obj["config"]
    reg = Registry(config.db_path)
    rows = reg.list_all(status=status, limit=limit)
    if not rows:
        console.print("[dim]レコードがありません。[/dim]")
        return
    q = JobQueue(config.db_path)
    table = Table(title=f"registry ({config.db_path})")
    table.add_column("ID", justify="right")
    table.add_column("状態")
    table.add_column("種別")
    table.add_column("index")
    table.add_column("タイトル / URL")
    table.add_column("パス")
    for r in rows:
        if r.status == "completed":
            ix = q.index_status_for_download(r.id)
            idx_label = (
                f"{ix['overall_pct']:5.1f}% "
                f"sc={ix['scenes']} clip={ix['clip_embeddings']} "
                f"txt={ix['text_embeddings']}"
            )
            if ix["message"] and ix["message"] not in ("indexed", "not indexed"):
                idx_label += f" ({ix['message']})"
        else:
            idx_label = "-"
        table.add_row(
            str(r.id),
            r.status,
            r.source_type,
            idx_label,
            (r.title or "") + ("\n" if r.title else "") + r.url,
            r.file_path or "-",
        )
    console.print(table)


@registry_group.command("status")
@click.argument("url")
@click.pass_context
def registry_status(ctx: click.Context, url: str) -> None:
    config: Config = ctx.obj["config"]
    reg = Registry(config.db_path)
    can = normalize_url(url)
    rec = reg.is_downloaded(can) or reg.find_by_url(url, completed_only=False)
    if not rec:
        console.print(f"[green]未ダウンロード[/green] {url}")
        return
    console.print(f"id={rec.id} status={rec.status}")
    console.print(f"  path: {rec.file_path}")
    console.print(f"  hash: {rec.file_hash}")
    q = JobQueue(config.db_path)
    scenes = q.list_scenes(rec.id)
    embs = q.list_embeddings(rec.id)
    console.print(f"  scenes: {len(scenes)}  embeddings: {len(embs)}")
    if rec.status == "completed":
        ix = q.index_status_for_download(rec.id)
        console.print(
            f"  index:  {ix['overall_pct']:.1f}% "
            f"(scene={ix['scene_pct']:.0f}% clip={ix['clip_pct']:.0f}% "
            f"embed={ix['embed_pct']:.0f}%) — {ix['message']}"
        )


@registry_group.command("show")
@click.argument("download_id", type=int)
@click.pass_context
def registry_show(ctx: click.Context, download_id: int) -> None:
    config: Config = ctx.obj["config"]
    reg = Registry(config.db_path)
    rec = reg.get(download_id)
    if not rec:
        console.print("[red]not found[/red]")
        sys.exit(1)
    console.print(f"id={rec.id} status={rec.status} type={rec.source_type}")
    console.print(f"url:  {rec.url}")
    console.print(f"path: {rec.file_path}")
    q = JobQueue(config.db_path)
    for s in q.list_scenes(download_id):
        console.print(
            f"  scene[{s['scene_index']}] {s['start_sec']:.2f}-{s['end_sec']:.2f}s "
            f"key={s.get('keyframe_path') or '-'}"
        )
    for e in q.list_embeddings(download_id):
        console.print(
            f"  emb modality={e['modality']} model={e['model']} chroma={e['chroma_id'][:8]}…"
        )


@registry_group.command("search")
@click.argument("query")
@click.option("-n", "--limit", default=8, show_default=True)
@click.option(
    "--modality",
    type=click.Choice(["text", "clip"], case_sensitive=False),
    default="text",
    show_default=True,
)
@click.pass_context
def registry_search(
    ctx: click.Context, query: str, limit: int, modality: str
) -> None:
    """テキストクエリでベクトル近傍検索（text=Ollama embed）"""
    config: Config = ctx.obj["config"]
    from vdl.index.embed import ollama_embed
    from vdl.vectorstore import VectorStore

    if modality == "text":
        emb = ollama_embed(query)
    else:
        console.print(
            "[yellow]clip modality のテキスト検索は未対応（キーフレーム画像が必要）[/yellow]"
        )
        sys.exit(2)

    store = VectorStore(config.chroma_dir)
    hits = store.query(emb, n_results=limit, modality=modality)
    if not hits:
        console.print("[dim]ヒットなし[/dim]")
        return
    table = Table(title=f"search: {query!r}")
    table.add_column("#", justify="right")
    table.add_column("dist")
    table.add_column("dl")
    table.add_column("scene")
    table.add_column("doc")
    for i, h in enumerate(hits, 1):
        meta = h.get("metadata") or {}
        table.add_row(
            str(i),
            f"{h.get('distance'):.4f}" if h.get("distance") is not None else "-",
            str(meta.get("download_id", "-")),
            str(meta.get("scene_index", "-")),
            (h.get("document") or "")[:80],
        )
    console.print(table)


# ---------------------------------------------------------------------------
# compat aliases + info
# ---------------------------------------------------------------------------


@main.command("list")
@click.option("--status", default=None)
@click.option("-n", "--limit", default=50)
@click.pass_context
def list_alias(ctx: click.Context, status: Optional[str], limit: int) -> None:
    """(alias) registry list"""
    ctx.invoke(registry_list, status=status, limit=limit)


@main.command("status")
@click.argument("url")
@click.pass_context
def status_alias(ctx: click.Context, url: str) -> None:
    """(alias) registry status"""
    ctx.invoke(registry_status, url=url)


@main.command("info")
@click.pass_context
def info_cmd(ctx: click.Context) -> None:
    """設定パスを表示"""
    config: Config = ctx.obj["config"]
    st = daemon_status(config)
    console.print(f"home:       {config.home}")
    console.print(f"downloads:  {config.download_dir}")
    console.print(f"registry:   {config.db_path}")
    console.print(f"chroma:     {config.chroma_dir}")
    console.print(f"keyframes:  {config.keyframes_dir}")
    console.print(f"temp:       {config.temp_dir}")
    console.print(f"daemon:     {'running pid='+str(st['pid']) if st['running'] else 'stopped'}")
    console.print(f"idle_sec:   {config.daemon_idle_sec} (VDL_DAEMON_IDLE_SEC)")
    console.print(f"workers:    {config.daemon_workers} (VDL_DAEMON_WORKERS)")
    console.print(f"queue:      {st['queue']}")


@main.command("open-dir")
@click.pass_context
def open_dir_cmd(ctx: click.Context) -> None:
    config: Config = ctx.obj["config"]
    config.ensure_dirs()
    click.echo(str(config.download_dir))


if __name__ == "__main__":
    main()
