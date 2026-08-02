"""vdl CLI entrypoint."""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Optional

import click
from rich.console import Console
from rich.table import Table

from vdl import __version__
from vdl.config import Config
from vdl.registry import Registry
from vdl.service import DownloadService
from vdl.util import normalize_url

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
    """vdl — URL / m3u8 / HTML から動画をダウンロード（ローカルレジストリ付き）"""
    ctx.ensure_object(dict)
    cfg = Config(home=home_dir) if home_dir else Config()
    cfg.ensure_dirs()
    ctx.obj["config"] = cfg


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
    help="HTML 候補の選択をスキップし、新規のみすべてダウンロード",
)
@click.option(
    "--all",
    "select_all",
    is_flag=True,
    help="HTML 候補をすべて対象（済は --force なしでスキップ）",
)
@click.option(
    "--timeout",
    default=60.0,
    show_default=True,
    type=float,
    help="HTTP タイムアウト秒",
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
) -> None:
    """URL を処理する（動画直DL / m3u8 / HTMLスクレイプ）"""
    config: Config = ctx.obj["config"]
    headers = _parse_header(header)
    with DownloadService(
        config,
        quality=quality,
        force=force,
        headers=headers,
        timeout=timeout,
    ) as svc:
        results = svc.process_url(url, select_all=select_all, auto_yes=auto_yes)

    failed = [r for r in results if r.status == "failed"]
    if failed:
        sys.exit(1)


@main.command("list")
@click.option(
    "--status",
    type=click.Choice(["completed", "failed", "pending", "skipped"], case_sensitive=False),
    default=None,
    help="ステータスで絞り込み",
)
@click.option("-n", "--limit", default=50, show_default=True, help="表示件数")
@click.pass_context
def list_cmd(ctx: click.Context, status: Optional[str], limit: int) -> None:
    """ダウンロード履歴（レジストリ）を一覧表示"""
    config: Config = ctx.obj["config"]
    reg = Registry(config.db_path)
    rows = reg.list_all(status=status, limit=limit)
    if not rows:
        console.print("[dim]レコードがありません。[/dim]")
        return

    table = Table(title=f"レジストリ ({config.db_path})")
    table.add_column("ID", style="bold", justify="right")
    table.add_column("状態")
    table.add_column("種別")
    table.add_column("タイトル / URL")
    table.add_column("パス")
    table.add_column("サイズ", justify="right")

    for r in rows:
        size = f"{r.file_size:,}" if r.file_size else "-"
        title = r.title or ""
        url_line = (title + "\n" if title else "") + r.url
        status_style = {
            "completed": "green",
            "failed": "red",
            "pending": "yellow",
            "skipped": "dim",
        }.get(r.status, "white")
        table.add_row(
            str(r.id),
            f"[{status_style}]{r.status}[/{status_style}]",
            r.source_type,
            url_line,
            r.file_path or "-",
            size,
        )
    console.print(table)


@main.command("status")
@click.argument("url")
@click.pass_context
def status_cmd(ctx: click.Context, url: str) -> None:
    """URL がレジストリに登録済みか確認"""
    config: Config = ctx.obj["config"]
    reg = Registry(config.db_path)
    can = normalize_url(url)
    rec = reg.is_downloaded(can) or reg.find_by_url(url, completed_only=False)
    if not rec:
        console.print(f"[green]未ダウンロード[/green] {url}")
        sys.exit(0)
    console.print(f"[yellow]登録あり[/yellow] id={rec.id} status={rec.status}")
    console.print(f"  url:      {rec.url}")
    console.print(f"  path:     {rec.file_path or '-'}")
    console.print(f"  type:     {rec.source_type}")
    console.print(f"  hash:     {rec.file_hash or '-'}")
    console.print(f"  m3u8 fp:  {rec.m3u8_fingerprint or '-'}")
    console.print(f"  updated:  {rec.updated_at}")
    if rec.error:
        console.print(f"  error:    {rec.error}")


@main.command("info")
@click.pass_context
def info_cmd(ctx: click.Context) -> None:
    """設定パスを表示"""
    config: Config = ctx.obj["config"]
    console.print(f"home:       {config.home}")
    console.print(f"downloads:  {config.download_dir}")
    console.print(f"registry:   {config.db_path}")
    console.print(f"temp:       {config.temp_dir}")


@main.command("open-dir")
@click.pass_context
def open_dir_cmd(ctx: click.Context) -> None:
    """ダウンロードディレクトリのパスを表示（cd 用）"""
    config: Config = ctx.obj["config"]
    config.ensure_dirs()
    click.echo(str(config.download_dir))


if __name__ == "__main__":
    main()
