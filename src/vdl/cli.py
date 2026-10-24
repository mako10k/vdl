"""vdl CLI — queue / daemon / registry + enqueue downloads."""

from __future__ import annotations

import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

import click
from rich.console import Console, Group
from rich.live import Live
from rich.panel import Panel
from rich.table import Table
from rich.text import Text

from vdl import __version__
from vdl.config import Config
from vdl.daemon import (
    daemon_status,
    ensure_daemon_running,
    run_forever,
    start_background,
    stop_daemon,
)
from vdl.queue import Job, JobKind, JobQueue
from vdl.registry import Registry
from vdl.supervisor import (
    ensure_runtime,
    ensure_supervisor_running,
    run_forever as supervisor_run_forever,
    start_background as supervisor_start_background,
    stop_supervisor,
    supervisor_status,
)
from vdl.util import normalize_url
from vdl.workers import enqueue_download, enqueue_index_chain, enqueue_startup_maintenance

console = Console()


def _parse_header(values: tuple[str, ...]) -> dict[str, str]:
    headers: dict[str, str] = {}
    for v in values:
        if ":" not in v:
            raise click.BadParameter(f"ヘッダは 'Name: Value' 形式: {v}")
        name, val = v.split(":", 1)
        headers[name.strip()] = val.strip()
    return headers


def _status_style(status: str) -> str:
    return {
        "pending": "yellow",
        "running": "cyan",
        "done": "green",
        "failed": "red",
        "cancelled": "dim",
        "completed": "green",
    }.get(status, "white")


def _jobs_table(
    rows: list[Job],
    *,
    title: str = "jobs",
    show_updated: bool = True,
) -> Table:
    table = Table(title=title, expand=True)
    table.add_column("ID", justify="right", style="bold")
    table.add_column("kind")
    table.add_column("pri", justify="right")
    table.add_column("status")
    table.add_column("progress", overflow="fold")
    table.add_column("dl_id", justify="right")
    table.add_column("dep", justify="right")
    if show_updated:
        table.add_column("updated")
    for j in rows:
        style = _status_style(j.status)
        cells = [
            str(j.id),
            j.kind,
            str(j.priority),
            f"[{style}]{j.status}[/{style}]",
            j.progress_label(),
            str(j.download_id or "-"),
            str(j.depends_on_job_id or "-"),
        ]
        if show_updated:
            cells.append(j.updated_at[:19] if j.updated_at else "-")
        table.add_row(*cells)
    return table


def _watch_dashboard(
    config: Config,
    *,
    limit: int = 20,
    active_only: bool = False,
    done_within_minutes: float | None = None,
    include_all_done: bool = False,
) -> Group:
    """Build a refreshable dashboard for `vdl watch`."""
    q = JobQueue(config.db_path)
    st = daemon_status(config)
    counts = q.counts(include_archived=False)
    n_arch = q.counts(include_archived=True).get("archived", 0)
    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
    done_mins = (
        float(done_within_minutes)
        if done_within_minutes is not None
        else float(config.list_done_minutes)
    )

    sup = supervisor_status(config)
    if sup["running"]:
        sup_line = Text.assemble(
            ("supervisor ", "dim"),
            ("running", "bold green"),
            (f" pid={sup['pid']}", ""),
        )
    else:
        sup_line = Text.assemble(
            ("supervisor ", "dim"),
            ("stopped", "yellow"),
        )
    if st["running"]:
        daemon_line = Text.assemble(
            ("worker    ", "dim"),
            ("running", "bold green"),
            (f" pid={st['pid']}", ""),
            (f"  workers={st.get('workers', config.daemon_workers)}", "dim"),
            (f"  idle_sec={st.get('idle_sec', config.daemon_idle_sec)}", "dim"),
        )
    else:
        daemon_line = Text.assemble(
            ("worker    ", "dim"),
            ("stopped", "bold yellow"),
            ("  (supervisor restarts when queue has work)", "dim"),
        )
    if st.get("reload_job_id"):
        reload_line = Text.assemble(
            ("self-update gate ", "bold magenta"),
            (f"job #{st['reload_job_id']}", "magenta"),
            (" — all other jobs wait on this", "dim"),
        )
    else:
        reload_line = Text(
            f"auto_reload={'on' if st.get('auto_reload', True) else 'off'}  "
            f"fp={st.get('code_fingerprint', '?')}",
            style="dim",
        )

    counts_parts = []
    for key in ("running", "pending", "done", "failed", "cancelled"):
        n = counts.get(key, 0)
        if n:
            counts_parts.append(f"[{_status_style(key)}]{key}={n}[/{_status_style(key)}]")
    if n_arch:
        counts_parts.append(f"[dim]archived={n_arch}[/dim]")
    counts_line = "  ".join(counts_parts) if counts_parts else "[dim]queue empty[/dim]"

    header = Panel(
        Group(
            Text(f"vdl watch  ·  {now}", style="bold"),
            sup_line,
            daemon_line,
            reload_line,
            Text.from_markup(f"queue  {counts_line}"),
        ),
        border_style="blue",
    )

    filter_meta: dict = {}
    if active_only:
        running = q.list_jobs(status="running", limit=limit)
        pending = q.list_jobs(status="pending", limit=limit)
        failed = q.list_jobs(status="failed", limit=min(10, limit))
        rows = (running + pending + failed)[:limit]
        title = f"active jobs (running+pending+failed, ≤{limit})"
    else:
        rows, filter_meta = q.list_jobs_display(
            limit=limit,
            done_within_minutes=done_mins,
            failed_within_minutes=config.list_failed_minutes,
            cancelled_within_minutes=config.list_cancelled_minutes,
            include_all_done=include_all_done,
        )
        title = f"jobs (≤{limit}"
        if filter_meta.get("filtered"):
            title += f", active+recent; hidden={filter_meta.get('hidden', 0)}"
        title += ")"

    if rows:
        body: Table | Text = _jobs_table(rows, title=title)
    else:
        body = Text("ジョブなし", style="dim")

    # Highlight in-flight only summary
    running_jobs = [j for j in rows if j.status == "running"]
    if running_jobs:
        run_table = Table(title="in flight", expand=True, show_header=True)
        run_table.add_column("ID", justify="right")
        run_table.add_column("kind")
        run_table.add_column("progress", overflow="fold")
        for j in running_jobs:
            run_table.add_row(str(j.id), j.kind, j.progress_label(bar_width=16))
        return Group(header, run_table, body)

    return Group(header, body)


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
    help="HTML 候補の選択をスキップし新規のみ対象（--enqueue と併用）",
)
@click.option(
    "--all",
    "select_all",
    is_flag=True,
    help="HTML 候補をすべて対象（--enqueue と併用）",
)
@click.option(
    "--enqueue",
    "-e",
    "do_enqueue",
    is_flag=True,
    help="HTML 候補をダウンロードジョブとしてキュー投入（オプトイン）",
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
    help="キューを使わず同期実行（動画直リンク / 明示的DL用）",
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
@click.option("--insecure", is_flag=True, help="TLS証明書の検証を無効化（期限切れ・自己署名などを許可）")
@click.pass_context
def get_cmd(
    ctx: click.Context,
    url: str,
    quality: str,
    force: bool,
    header: tuple[str, ...],
    auto_yes: bool,
    select_all: bool,
    do_enqueue: bool,
    timeout: float,
    sync: bool,
    no_index: bool,
    no_daemon: bool,
    insecure: bool,
) -> None:
    """
    URL を処理する。

    - 動画 / m3u8: 既定でダウンロードジョブをキュー投入
    - HTML ページ: 既定で候補一覧のみ（--enqueue でジョブ投入をオプトイン）
    """
    config: Config = ctx.obj["config"]
    headers = _parse_header(header)

    from vdl.classifier import ResourceKind, ResourceProbe
    from vdl.service import DownloadService
    import httpx

    # Probe to branch HTML vs media
    kind = None
    try:
        with httpx.Client(follow_redirects=True, timeout=timeout, verify=not insecure) as client:
            probe = ResourceProbe(client, timeout=timeout)
            classified = probe.probe(url, extra_headers=headers or None)
            kind = classified.kind
            console.print(
                f"[dim]分類:[/dim] {kind.value}  "
                f"[dim]→[/dim] {classified.final_url}"
            )
    except Exception as e:
        console.print(f"[yellow]分類プローブ失敗[/yellow] {e} — ダウンロードジョブとして投入します")

    # HTML / unknown → candidates list (enqueue is opt-in)
    if kind in (ResourceKind.HTML, ResourceKind.UNKNOWN):
        _run_html_get(
            config,
            url,
            headers=headers,
            quality=quality,
            force=force,
            timeout=timeout,
            insecure=insecure,
            auto_yes=auto_yes,
            select_all=select_all,
            do_enqueue=do_enqueue,
            no_index=no_index,
            no_daemon=no_daemon,
            sync=sync,
        )
        return

    # Direct media: video or index (or probe failed → treat as download job)
    if sync:
        with DownloadService(
            config,
            quality=quality,
            force=force,
            headers=headers,
            timeout=timeout,
            insecure=insecure,
        ) as svc:
            results = svc.process_url(
                url,
                select_all=False,
                auto_yes=False,
                list_only=False,
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
        auto_yes=False,
        select_all=False,
        timeout=timeout,
        insecure=insecure,
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


def _run_html_get(
    config: Config,
    url: str,
    *,
    headers: dict[str, str],
    quality: str,
    force: bool,
    timeout: float,
    auto_yes: bool,
    select_all: bool,
    do_enqueue: bool,
    no_index: bool,
    no_daemon: bool,
    insecure: bool,
    sync: bool,
) -> None:
    """List HTML media candidates; optionally enqueue/sync-download them."""
    from vdl.service import DownloadService

    with DownloadService(
        config,
        quality=quality,
        force=force,
        headers=headers,
        timeout=timeout,
        insecure=insecure,
    ) as svc:
        try:
            page_url, rows = svc.fetch_html_candidates(url)
        except Exception as e:
            console.print(f"[red]ページ取得失敗[/red] {e}")
            sys.exit(1)

        if not rows:
            console.print("[yellow]動画・インデックス候補が見つかりませんでした。[/yellow]")
            return

        svc.print_candidates_table(page_url, rows)

        if not do_enqueue and not sync:
            console.print(
                f"[dim]候補 {len(rows)} 件を表示しました。"
                " ジョブ投入は --enqueue（-e）を付けて再実行してください。[/dim]"
            )
            return

        # Enqueue or sync-download selected candidates
        indices = svc.select_candidate_indices(
            rows,
            select_all=select_all,
            auto_yes=auto_yes or select_all,
            interactive=not (auto_yes or select_all),
        )
        if indices is None:
            console.print("[dim]キャンセル[/dim]")
            return
        if not indices:
            console.print("[dim]対象候補がありません[/dim]")
            return

        if sync:
            for i in indices:
                row = rows[i]
                c = row.candidate
                if row.known and not force:
                    console.print(
                        f"[yellow]#{i+1} スキップ[/yellow] "
                        f"{row.state_label} id={row.download_id}"
                    )
                    continue
                console.print(f"[bold]#{i+1} sync DL[/bold] {c.url}")
                results = svc.process_url(
                    c.url,
                    parent_page_url=page_url,
                    list_only=False,
                )
                if not no_index:
                    q = JobQueue(config.db_path)
                    for r in results:
                        if r.record_id and r.status in ("downloaded", "skipped"):
                            enqueue_index_chain(q, r.record_id)
            if not no_daemon and not no_index:
                _auto_start_daemon(config)
            return

        q = JobQueue(config.db_path)
        # within this batch + live queue: avoid simultaneous duplicates
        seen_batch: set[str] = set()
        job_ids: list[int] = []
        for i in indices:
            row = rows[i]
            c = row.candidate
            nkey = normalize_url(c.url)
            if nkey in seen_batch:
                console.print(f"[yellow]#{i+1} スキップ[/yellow] 同一抽出内の重複")
                continue
            if row.known and not force:
                console.print(
                    f"[yellow]#{i+1} スキップ[/yellow] "
                    f"{row.state_label} id={row.download_id} {c.url}"
                )
                continue
            # re-check active queue (another process may have enqueued)
            active = q.active_download_urls()
            if nkey in active and not force:
                console.print(f"[yellow]#{i+1} スキップ[/yellow] 既にキュー中 {c.url}")
                continue
            seen_batch.add(nkey)
            job = enqueue_download(
                q,
                c.url,
                quality=quality,
                force=force,
                headers=headers,
                timeout=timeout,
                insecure=insecure,
                no_index=no_index,
            )
            job_ids.append(job.id)
            console.print(
                f"[green]queued[/green] #{i+1} job_id={job.id} [{c.kind}] {c.url}"
            )

        if not job_ids:
            console.print("[dim]新規ジョブはありませんでした[/dim]")
            return
        console.print(f"[dim]enqueued {len(job_ids)} download job(s) from page[/dim]")
        if not no_daemon:
            _auto_start_daemon(config)


@main.command("candidates")
@click.argument("url")
@click.option(
    "-H",
    "--header",
    multiple=True,
    help="追加 HTTP ヘッダ",
)
@click.option(
    "--enqueue",
    "-e",
    "do_enqueue",
    is_flag=True,
    help="選択した候補をダウンロードジョブとして投入（オプトイン）",
)
@click.option(
    "-y",
    "--yes",
    "auto_yes",
    is_flag=True,
    help="新規候補をすべて対象（--enqueue 時）",
)
@click.option(
    "--all",
    "select_all",
    is_flag=True,
    help="全候補を対象（--enqueue 時）",
)
@click.option("-f", "--force", is_flag=True, help="済でも再取得ジョブを投入")
@click.option("-q", "--quality", default="best", show_default=True)
@click.option("--timeout", default=60.0, show_default=True, type=float)
@click.option("--no-index", is_flag=True)
@click.option("--no-daemon", is_flag=True)
@click.option("--insecure", is_flag=True, help="TLS証明書の検証を無効化")
@click.pass_context
def candidates_cmd(
    ctx: click.Context,
    url: str,
    header: tuple[str, ...],
    do_enqueue: bool,
    auto_yes: bool,
    select_all: bool,
    force: bool,
    quality: str,
    timeout: float,
    no_index: bool,
    no_daemon: bool,
    insecure: bool,
) -> None:
    """HTML ページから動画 / m3u8 候補を抽出して表示する。"""
    config: Config = ctx.obj["config"]
    headers = _parse_header(header)
    _run_html_get(
        config,
        url,
        headers=headers,
        quality=quality,
        force=force,
        timeout=timeout,
        insecure=insecure,
        auto_yes=auto_yes,
        select_all=select_all,
        do_enqueue=do_enqueue,
        no_index=no_index,
        no_daemon=no_daemon,
        sync=False,
    )


def _auto_start_daemon(config: Config) -> None:
    """Start supervisor (preferred) or worker; supervisor keeps worker alive."""
    try:
        role, pid, started = ensure_runtime(config)
    except Exception as e:
        console.print(f"[red]runtime 自動起動失敗[/red] {e}")
        console.print(
            "[dim]`vdl supervisor start` または `vdl daemon start` を実行してください[/dim]"
        )
        return
    if role == "supervisor":
        idle = getattr(config, "supervisor_idle_sec", 120)
        if started:
            console.print(
                f"[cyan]supervisor auto-started[/cyan] pid={pid} "
                f"(keeps worker alive; idle exit after {idle:g}s)"
            )
        else:
            console.print(f"[dim]supervisor running pid={pid}[/dim]")
        # Show worker status too
        wst = daemon_status(config)
        if wst["running"]:
            console.print(f"[dim]worker running pid={wst['pid']}[/dim]")
        else:
            console.print("[dim]worker will start when supervisor sees queue work[/dim]")
    else:
        idle = getattr(config, "daemon_idle_sec", 60)
        workers = getattr(config, "daemon_workers", 4)
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
        ["pending", "running", "done", "failed", "cancelled", "archived"],
        case_sensitive=False,
    ),
    default=None,
)
@click.option(
    "--kind",
    type=click.Choice(
        [k.value for k in JobKind],
        case_sensitive=False,
    ),
    default=None,
)
@click.option(
    "-n",
    "--limit",
    default=None,
    type=int,
    help="表示上限 (default: VDL_LIST_LIMIT / 30)",
)
@click.option(
    "--done-within",
    "done_within",
    default=None,
    type=float,
    help="done を残す分数 (default: VDL_LIST_DONE_MINUTES / 10)",
)
@click.option(
    "--all-done",
    "include_all_done",
    is_flag=True,
    help="時間フィルタを外し status 指定なしで広く表示（limit 内）",
)
@click.pass_context
def queue_list(
    ctx: click.Context,
    status: Optional[str],
    kind: Optional[str],
    limit: Optional[int],
    done_within: Optional[float],
    include_all_done: bool,
) -> None:
    config: Config = ctx.obj["config"]
    q = JobQueue(config.db_path)
    lim = int(limit if limit is not None else config.list_limit)
    mins = float(
        done_within if done_within is not None else config.list_done_minutes
    )
    rows, meta = q.list_jobs_display(
        status=status,
        kind=kind,
        limit=lim,
        done_within_minutes=mins,
        failed_within_minutes=config.list_failed_minutes,
        cancelled_within_minutes=config.list_cancelled_minutes,
        include_all_done=include_all_done
        or (status in ("done", "failed", "cancelled", "archived") if status else False),
    )
    counts = q.counts(include_archived=True)
    active = q.counts(include_archived=False)
    console.print(f"[dim]active={active}  all={counts}[/dim]")
    if meta.get("filtered"):
        hd = meta.get("hidden_detail") or {}
        console.print(
            f"[dim]filter: run/pending + done≤{mins:g}m "
            f"failed≤{meta.get('failed_within_minutes', 30):g}m "
            f"cancelled≤{meta.get('cancelled_within_minutes', 10):g}m; "
            f"hidden≈{meta.get('hidden', 0)} {hd}; limit={lim} "
            f"(--all-done / --status で履歴)[/dim]"
        )
    if not rows:
        console.print("[dim]有効なジョブなし（アーカイブ済みは --status archived）[/dim]")
        return
    console.print(_jobs_table(rows))


@queue_group.command("housekeep")
@click.option(
    "--archive-after",
    default=None,
    type=float,
    help="終了ジョブをアーカイブする分数 (default: VDL_JOB_ARCHIVE_MINUTES)",
)
@click.option(
    "--retry-after",
    default=None,
    type=float,
    help="失敗ジョブの自動リトライまでの待ち分数",
)
@click.option("--no-retry", is_flag=True, help="アーカイブ前の自動リトライをしない")
@click.pass_context
def queue_housekeep(
    ctx: click.Context,
    archive_after: Optional[float],
    retry_after: Optional[float],
    no_retry: bool,
) -> None:
    """失敗の自動リトライ（対象のみ）と、古い終了ジョブのアーカイブ。"""
    config: Config = ctx.obj["config"]
    q = JobQueue(config.db_path)
    stats = q.housekeep(
        archive_after_minutes=float(
            archive_after
            if archive_after is not None
            else config.job_archive_minutes
        ),
        retry_after_minutes=float(
            retry_after
            if retry_after is not None
            else config.job_housekeep_retry_after_min
        ),
        auto_retry=not no_retry and config.job_housekeep_auto_retry,
        max_attempts=config.job_max_attempts,
    )
    console.print(
        f"[green]housekeep[/green] retried={stats['retried']} "
        f"archived={stats['archived']} orphans={stats['orphans_cancelled']}"
    )
    if stats["retry_ids"]:
        console.print(f"  retry ids: {stats['retry_ids'][:20]}")
    if stats["archived_ids"]:
        console.print(f"  archived ids: {stats['archived_ids'][:20]}…")


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
    # Surface artifacts / failures clearly
    res = job.result or {}
    paths = res.get("paths") or []
    if not paths:
        for r in res.get("results") or []:
            if r.get("path"):
                paths.append(r["path"])
            elif isinstance(r.get("message"), str) and r["message"].startswith("/"):
                paths.append(r["message"])
    if paths:
        console.print("[green]artifact:[/green]")
        for p in paths:
            console.print(f"  {p}")
    if job.download_id:
        reg = Registry(config.db_path)
        rec = reg.get(job.download_id)
        if rec:
            console.print(
                f"registry: id={rec.id} status={rec.status} "
                f"size={rec.file_size or '-'} path={rec.file_path or '-'}"
            )
            if rec.error:
                console.print(f"[red]  reg error: {rec.error}[/red]")
    if res.get("failed"):
        console.print("[yellow]partial failures:[/yellow]")
        for f in res["failed"]:
            console.print(f"  {f}")
    console.print(f"payload:  {job.payload}")
    console.print(f"result:   {job.result}")
    if job.error:
        console.print(f"[red]error:    {job.error}[/red]")
    if job.status == "failed":
        console.print("[dim]retry: vdl queue retry " + str(job.id) + "[/dim]")


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
    if job.status == "pending":
        _auto_start_daemon(config)


@queue_group.command("maintain")
@click.option("--no-daemon", is_flag=True, help="デーモン自動起動しない")
@click.pass_context
def queue_maintain(ctx: click.Context, no_daemon: bool) -> None:
    """重複除去と .ts→.mp4 をキューに載せる（API 起動時と同じ）"""
    config: Config = ctx.obj["config"]
    q = JobQueue(config.db_path)
    jobs = enqueue_startup_maintenance(q)
    remux = jobs["ts_to_mp4"]
    dedup = jobs["dedup"]
    console.print(
        f"[green]maintenance queued[/green] "
        f"ts_to_mp4={remux.id} dedup={dedup.id} (depends on {dedup.depends_on_job_id})"
    )
    if not no_daemon:
        _auto_start_daemon(config)


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
    help="総枠数 (最小2、後処理1＋残りDL、default: VDL_DAEMON_WORKERS または 4)",
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
# supervisor — keeps worker daemon alive
# ---------------------------------------------------------------------------


@main.group("supervisor")
def supervisor_group() -> None:
    """ワーカーデーモンの監視・自動再起動（ジョブが残っている間）"""


@supervisor_group.command("run")
@click.option("--interval", default=1.0, show_default=True, type=float)
@click.option("--pid", "write_pid", is_flag=True, help="PID ファイルを書く")
@click.option(
    "--idle",
    "idle_sec",
    type=float,
    default=None,
    help="キュー空+worker停止のままこの秒数で終了 (0=無効)",
)
@click.pass_context
def supervisor_run(
    ctx: click.Context,
    interval: float,
    write_pid: bool,
    idle_sec: float | None,
) -> None:
    """フォアグラウンドでスーパーバイザを実行"""
    config: Config = ctx.obj["config"]
    if idle_sec is None:
        effective = float(config.supervisor_idle_sec) if write_pid else 0.0
    else:
        effective = float(idle_sec)
    supervisor_run_forever(
        config,
        poll_interval=interval,
        foreground=True,
        write_pid_file=write_pid,
        idle_sec=effective,
    )


@supervisor_group.command("start")
@click.pass_context
def supervisor_start(ctx: click.Context) -> None:
    """バックグラウンドでスーパーバイザ起動"""
    config: Config = ctx.obj["config"]
    try:
        pid = supervisor_start_background(config)
    except RuntimeError as e:
        console.print(f"[yellow]{e}[/yellow]")
        sys.exit(1)
    console.print(f"[green]supervisor started[/green] pid={pid}")
    console.print(f"  log: {config.supervisor_log_path}")


@supervisor_group.command("stop")
@click.option(
    "--with-worker",
    is_flag=True,
    help="ワーカーデーモンも停止する",
)
@click.pass_context
def supervisor_stop(ctx: click.Context, with_worker: bool) -> None:
    config: Config = ctx.obj["config"]
    ok = stop_supervisor(config, stop_worker=with_worker)
    if ok:
        console.print("[green]supervisor stopped[/green]")
    else:
        console.print("[dim]supervisor was not running[/dim]")
    if with_worker:
        console.print("[dim]worker stop requested[/dim]")


@supervisor_group.command("status")
@click.pass_context
def supervisor_status_cmd(ctx: click.Context) -> None:
    config: Config = ctx.obj["config"]
    st = supervisor_status(config)
    if st["running"]:
        console.print(f"[green]running[/green] pid={st['pid']}")
    else:
        console.print("[yellow]stopped[/yellow]")
    console.print(f"  pid_file: {st['pid_file']}")
    console.print(f"  log_file: {st['log_file']}")
    console.print(f"  idle_sec: {st.get('idle_sec')}")
    w = st.get("worker") or {}
    if w.get("running"):
        console.print(f"  worker:   running pid={w.get('pid')}")
    else:
        console.print("  worker:   stopped")
    console.print(f"  queue:    {st.get('queue')}")


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
    "--scenes",
    "by_scene",
    is_flag=True,
    help="download 単位に集約せずシーン単位で表示",
)
@click.pass_context
def registry_search(
    ctx: click.Context, query: str, limit: int, by_scene: bool
) -> None:
    """テキスト類似検索（alias: vdl search text）"""
    ctx.invoke(search_text_cmd, query=query, limit=limit, by_scene=by_scene)


@registry_group.command("similar")
@click.argument("download_id", type=int)
@click.option("-n", "--limit", default=8, show_default=True)
@click.option(
    "--modality",
    type=click.Choice(["clip", "text"], case_sensitive=False),
    default="clip",
    show_default=True,
    help="clip=見た目類似 / text=テキスト埋め込み類似",
)
@click.option("--scene", "scene_index", type=int, default=None, help="基準シーン index")
@click.option(
    "--scenes",
    "by_scene",
    is_flag=True,
    help="download 単位に集約せずシーン単位で表示",
)
@click.pass_context
def registry_similar(
    ctx: click.Context,
    download_id: int,
    limit: int,
    modality: str,
    scene_index: int | None,
    by_scene: bool,
) -> None:
    """レジストリ動画との類似検索（alias: vdl search similar）"""
    ctx.invoke(
        search_similar_cmd,
        download_id=download_id,
        limit=limit,
        modality=modality,
        scene_index=scene_index,
        by_scene=by_scene,
    )


# ---------------------------------------------------------------------------
# search — text & similar
# ---------------------------------------------------------------------------


def _print_search_hits(hits, *, title: str) -> None:
    if not hits:
        console.print("[dim]ヒットなし[/dim]")
        return
    table = Table(title=title, expand=True)
    table.add_column("#", justify="right")
    table.add_column("score", justify="right")
    table.add_column("dl", justify="right")
    table.add_column("scene", justify="right")
    table.add_column("time")
    table.add_column("title / path")
    table.add_column("doc", overflow="fold")
    for h in hits:
        time_s = "-"
        if h.start_sec is not None and h.end_sec is not None:
            time_s = f"{h.start_sec:.1f}-{h.end_sec:.1f}s"
        title_path = h.title or ""
        if h.path:
            title_path = (title_path + "\n" if title_path else "") + h.path
        table.add_row(
            str(h.rank),
            f"{h.score:.3f}",
            str(h.download_id if h.download_id >= 0 else "-"),
            str(h.scene_index if h.scene_index is not None else "-"),
            time_s,
            title_path or "-",
            (h.document or "")[:100],
        )
    console.print(table)


@main.group("search")
def search_group() -> None:
    """動画のテキスト検索 / レジストリ指定の類似検索"""


@search_group.command("text")
@click.argument("query")
@click.option("-n", "--limit", default=8, show_default=True)
@click.option(
    "--scenes",
    "by_scene",
    is_flag=True,
    help="download 単位に集約せずシーン単位で表示",
)
@click.option(
    "--modality",
    type=click.Choice(["clip", "text"], case_sensitive=False),
    default="clip",
    show_default=True,
    help="clip=見た目（CLIP・既定）/ text=メタデータ（Ollama・ファイル名寄り）",
)
@click.pass_context
def search_text_cmd(
    ctx: click.Context, query: str, limit: int, by_scene: bool, modality: str
) -> None:
    """テキストクエリで検索（既定: CLIP オープン語彙の視覚検索）"""
    config: Config = ctx.obj["config"]
    from vdl.search import SearchService

    try:
        out = SearchService(config).search_text_ex(
            query,
            limit=limit,
            by_download=not by_scene,
            modality=modality,  # type: ignore[arg-type]
        )
    except Exception as e:
        console.print(f"[red]検索失敗[/red] {e}")
        sys.exit(1)
    title = (
        f"text search [{out.modality}]: {query!r}"
        + (" (scenes)" if by_scene else " (by download)")
    )
    if out.translated:
        console.print(f"[dim]CLIP query (EN): {out.query_used!r}[/dim]")
    for note in out.notes:
        console.print(f"[dim]{note}[/dim]")
    _print_search_hits(out.hits, title=title)


@search_group.command("similar")
@click.argument("download_id", type=int)
@click.option("-n", "--limit", default=8, show_default=True)
@click.option(
    "--modality",
    type=click.Choice(["clip", "text"], case_sensitive=False),
    default="clip",
    show_default=True,
    help="clip=見た目 / text=テキスト埋め込み",
)
@click.option(
    "--scene",
    "scene_index",
    type=int,
    default=None,
    help="基準にするシーン index（省略時は全シーン平均）",
)
@click.option(
    "--scenes",
    "by_scene",
    is_flag=True,
    help="download 単位に集約せずシーン単位で表示",
)
@click.pass_context
def search_similar_cmd(
    ctx: click.Context,
    download_id: int,
    limit: int,
    modality: str,
    scene_index: int | None,
    by_scene: bool,
) -> None:
    """レジストリの download_id を基準に似ている動画を上位表示"""
    config: Config = ctx.obj["config"]
    from vdl.search import SearchService

    try:
        hits = SearchService(config).search_similar(
            download_id,
            scene_index=scene_index,
            limit=limit,
            modality=modality,  # type: ignore[arg-type]
            by_download=not by_scene,
        )
    except Exception as e:
        console.print(f"[red]類似検索失敗[/red] {e}")
        sys.exit(1)
    scope = f"dl={download_id}"
    if scene_index is not None:
        scope += f" scene={scene_index}"
    _print_search_hits(
        hits,
        title=f"similar to {scope} [{modality}]"
        + (" (scenes)" if by_scene else " (by download)"),
    )


# ---------------------------------------------------------------------------
# watch — live queue / progress dashboard
# ---------------------------------------------------------------------------


@main.command("watch")
@click.option(
    "-n",
    "--interval",
    default=2.0,
    show_default=True,
    type=float,
    help="更新間隔（秒）。ETA は表示のたびに現在時刻から再計算",
)
@click.option(
    "-l",
    "--limit",
    default=None,
    type=int,
    help="表示するジョブ件数 (default: VDL_LIST_LIMIT)",
)
@click.option(
    "--active",
    "active_only",
    is_flag=True,
    help="running / pending / failed のみ表示",
)
@click.option(
    "--all-done",
    "include_all_done",
    is_flag=True,
    help="古い done も表示",
)
@click.option(
    "--done-within",
    "done_within",
    default=None,
    type=float,
    help="done を残す分数",
)
@click.option(
    "--until-idle",
    is_flag=True,
    help="キューが空（pending/running=0）になったら終了",
)
@click.pass_context
def watch_cmd(
    ctx: click.Context,
    interval: float,
    limit: Optional[int],
    active_only: bool,
    include_all_done: bool,
    done_within: Optional[float],
    until_idle: bool,
) -> None:
    """
    キューと進捗をライブ表示する（Ctrl+C で終了）。

    例:
      vdl watch
      vdl watch --active -n 0.5
      vdl watch --until-idle
    """
    config: Config = ctx.obj["config"]
    interval = max(0.2, float(interval))
    lim = int(limit if limit is not None else config.list_limit)
    mins = float(
        done_within if done_within is not None else config.list_done_minutes
    )

    def _dash() -> Group:
        return _watch_dashboard(
            config,
            limit=lim,
            active_only=active_only,
            done_within_minutes=mins,
            include_all_done=include_all_done,
        )

    try:
        with Live(
            _dash(),
            console=console,
            refresh_per_second=max(1, int(1 / interval) if interval < 1 else 2),
            screen=False,
        ) as live:
            while True:
                time.sleep(interval)
                live.update(_dash())
                if until_idle:
                    counts = JobQueue(config.db_path).counts()
                    if counts.get("pending", 0) == 0 and counts.get("running", 0) == 0:
                        console.print("[green]queue idle — watch done[/green]")
                        break
    except KeyboardInterrupt:
        console.print("\n[dim]watch stopped[/dim]")


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
    sup = supervisor_status(config)
    console.print(
        f"supervisor: {'running pid='+str(sup['pid']) if sup['running'] else 'stopped'}"
        f" (VDL_SUPERVISOR={'on' if config.supervisor_enabled else 'off'})"
    )
    console.print(f"worker:     {'running pid='+str(st['pid']) if st['running'] else 'stopped'}")
    console.print(f"idle_sec:   worker={config.daemon_idle_sec} supervisor={config.supervisor_idle_sec}")
    console.print(f"workers:    {config.daemon_workers} (VDL_DAEMON_WORKERS)")
    console.print(f"auto_reload:{'on' if config.daemon_auto_reload else 'off'} (VDL_DAEMON_AUTO_RELOAD)")
    if st.get("reload_job_id"):
        console.print(f"reload_gate:job #{st['reload_job_id']}")
    console.print(f"code_fp:    {st.get('code_fingerprint')}")
    console.print(f"queue:      {st['queue']}")


@main.command("open-dir")
@click.pass_context
def open_dir_cmd(ctx: click.Context) -> None:
    config: Config = ctx.obj["config"]
    config.ensure_dirs()
    click.echo(str(config.download_dir))


if __name__ == "__main__":
    main()
