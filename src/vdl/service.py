"""Orchestrate: classify URL → registry check → download / scrape / select."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Optional

import httpx
from rich.console import Console
from rich.table import Table

from vdl.classifier import ClassifiedResource, ResourceKind, ResourceProbe
from vdl.config import Config
from vdl.downloader import ProgressCallback, download_file
from vdl.m3u8_dl import (
    UnsupportedIndexError,
    download_m3u8_ffmpeg,
    parse_m3u8,
)
from vdl.registry import Registry, SourceType
from vdl.scraper import MediaCandidate, extract_candidates
from vdl.util import (
    filename_from_url,
    is_index_extension,
    is_mpd_url,
    is_video_extension,
    normalize_url,
    resolve_local_path,
    sha256_file,
)


console = Console()


@dataclass
class ActionResult:
    status: str  # downloaded | skipped | failed | listed
    message: str
    path: Optional[Path] = None
    record_id: Optional[int] = None


class DownloadService:
    def __init__(
        self,
        config: Config | None = None,
        *,
        quality: str = "best",
        force: bool = False,
        headers: dict[str, str] | None = None,
        timeout: float = 60.0,
        on_progress: ProgressCallback | None = None,
        on_phase: Callable[[str, float | None], None] | None = None,
    ) -> None:
        self.config = config or Config()
        self.config.ensure_dirs()
        self.registry = Registry(self.config.db_path)
        self.quality = quality
        self.force = force
        self.headers = headers or {}
        self.timeout = timeout
        # on_progress(written, total) for direct file downloads
        self.on_progress = on_progress
        # on_phase(message, pct_or_None) for multi-step downloads (probe, hls, …)
        self.on_phase = on_phase
        self.client = httpx.Client(
            follow_redirects=True,
            timeout=timeout,
            headers={"User-Agent": "Mozilla/5.0 (compatible; vdl/0.1)"},
        )

    def _phase(self, message: str, pct: float | None = None) -> None:
        if self.on_phase:
            self.on_phase(message, pct)

    def close(self) -> None:
        self.client.close()

    def __enter__(self) -> "DownloadService":
        return self

    def __exit__(self, *args: object) -> None:
        self.close()

    # ------------------------------------------------------------------
    # Public entry
    # ------------------------------------------------------------------

    def process_url(
        self,
        url: str,
        *,
        parent_page_url: str | None = None,
        source_type: SourceType | None = None,
        select_all: bool = False,
        auto_yes: bool = False,
    ) -> list[ActionResult]:
        url = url.strip()
        self._phase("classifying", 1.0)

        # Local filesystem path / file:// (PR1)
        local = resolve_local_path(url)
        if local is not None:
            return self._handle_local_path(
                local,
                original=url,
                parent_page_url=parent_page_url,
                source_type=source_type,
            )

        # MPD early reject (D3) — even before probe
        if is_mpd_url(url):
            msg = "MPEG-DASH (.mpd) は MVP 対象外です。HLS (.m3u8) のみ対応しています。"
            console.print(f"[red]非対応[/red] {msg}")
            return [ActionResult(status="failed", message=msg)]

        probe = ResourceProbe(self.client, timeout=self.timeout)
        classified = probe.probe(url, extra_headers=self.headers or None)

        console.print(
            f"[dim]分類:[/dim] {classified.kind.value}  "
            f"[dim]type=[/dim]{classified.content_type or '-'}  "
            f"[dim]→[/dim] {classified.final_url}"
        )

        if classified.kind == ResourceKind.VIDEO:
            return [
                self._handle_video(
                    classified,
                    parent_page_url=parent_page_url,
                    source_type=source_type or SourceType.DIRECT,
                )
            ]

        if classified.kind == ResourceKind.INDEX:
            if is_mpd_url(classified.final_url) or is_mpd_url(classified.url):
                msg = "MPEG-DASH (.mpd) は MVP 対象外です。HLS (.m3u8) のみ対応しています。"
                console.print(f"[red]非対応[/red] {msg}")
                return [ActionResult(status="failed", message=msg)]
            return [
                self._handle_m3u8(
                    classified,
                    parent_page_url=parent_page_url,
                    source_type=source_type or SourceType.M3U8,
                )
            ]

        if classified.kind == ResourceKind.HTML:
            return self._handle_html(
                classified, select_all=select_all, auto_yes=auto_yes
            )

        # UNKNOWN: try as HTML fetch (design D1)
        console.print("[yellow]種別を判定できませんでした。HTML として取得を試みます。[/yellow]")
        return self._handle_html(classified, select_all=select_all, auto_yes=auto_yes)

    def _handle_local_path(
        self,
        path: Path,
        *,
        original: str,
        parent_page_url: str | None,
        source_type: SourceType | None,
    ) -> list[ActionResult]:
        if not path.exists():
            msg = f"ローカルパスが見つかりません: {path}"
            console.print(f"[red]失敗[/red] {msg}")
            return [ActionResult(status="failed", message=msg)]

        name = path.name
        if is_mpd_url(name) or path.suffix.lower() == ".mpd":
            msg = "MPEG-DASH (.mpd) は MVP 対象外です。HLS (.m3u8) のみ対応しています。"
            console.print(f"[red]非対応[/red] {msg}")
            return [ActionResult(status="failed", message=msg)]

        if is_index_extension(name) or path.suffix.lower() in {".m3u8", ".m3u"}:
            console.print(f"[dim]分類:[/dim] index (local)  → {path}")
            fake = ClassifiedResource(
                url=original,
                canonical_url=normalize_url(path.as_uri()),
                kind=ResourceKind.INDEX,
                content_type="application/vnd.apple.mpegurl",
                content_length=path.stat().st_size,
                final_url=path.as_uri(),
                filename_hint=path.stem + ".mp4",
            )
            return [
                self._handle_m3u8(
                    fake,
                    parent_page_url=parent_page_url,
                    source_type=source_type or SourceType.M3U8,
                    local_path=path,
                )
            ]

        if is_video_extension(name):
            # Copy local video into download dir + register
            console.print(f"[dim]分類:[/dim] video (local)  → {path}")
            return [self._handle_local_video(path, original=original)]

        msg = f"対応していないローカルファイルです: {path} (動画または .m3u8 を指定してください)"
        console.print(f"[red]失敗[/red] {msg}")
        return [ActionResult(status="failed", message=msg)]

    def _handle_local_video(self, path: Path, *, original: str) -> ActionResult:
        import shutil

        from vdl.util import unique_path

        canonical = normalize_url(path.as_uri())
        existing = self.registry.is_downloaded(canonical)
        if existing and not self.force:
            console.print(
                f"[yellow]スキップ[/yellow] 既にダウンロード済み "
                f"(id={existing.id}, path={existing.file_path})"
            )
            return ActionResult(
                status="skipped",
                message=f"既にダウンロード済み: {existing.file_path}",
                path=Path(existing.file_path) if existing.file_path else None,
                record_id=existing.id,
            )

        rec = self.registry.create(
            original,
            canonical_url=canonical,
            source_type=SourceType.DIRECT,
            title=path.name,
        )
        try:
            dest = unique_path(self.config.download_dir, path.name)
            shutil.copy2(path, dest)
            digest = sha256_file(dest)
            size = dest.stat().st_size
            rec = self.registry.mark_completed(
                rec.id,
                file_path=str(dest),
                file_size=size,
                file_hash=digest,
            )
            console.print(f"[green]完了[/green] {dest} ({size:,} bytes)")
            return ActionResult(
                status="downloaded",
                message=str(dest),
                path=dest,
                record_id=rec.id,
            )
        except Exception as e:
            self.registry.mark_failed(rec.id, str(e))
            console.print(f"[red]失敗[/red] {e}")
            return ActionResult(status="failed", message=str(e), record_id=rec.id)

    # ------------------------------------------------------------------
    # Video direct
    # ------------------------------------------------------------------

    def _handle_video(
        self,
        res: ClassifiedResource,
        *,
        parent_page_url: str | None,
        source_type: SourceType,
    ) -> ActionResult:
        target = res.final_url or res.url
        canonical = res.canonical_url or normalize_url(target)

        existing = self.registry.is_downloaded(canonical)
        if existing and not self.force:
            path = existing.file_path or "?"
            console.print(
                f"[yellow]スキップ[/yellow] 既にダウンロード済み "
                f"(id={existing.id}, path={path})"
            )
            return ActionResult(
                status="skipped",
                message=f"既にダウンロード済み: {path}",
                path=Path(path) if existing.file_path else None,
                record_id=existing.id,
            )

        rec = self.registry.create(
            target,
            canonical_url=canonical,
            source_type=source_type,
            content_type=res.content_type,
            parent_page_url=parent_page_url,
            title=res.filename_hint or filename_from_url(target),
        )
        try:
            self._phase("downloading", 5.0)

            def _dl_prog(written: int, total: int | None) -> None:
                if self.on_progress:
                    self.on_progress(written, total)
                if total and total > 0 and self.on_phase:
                    # map byte progress into 5–95%
                    pct = 5.0 + 90.0 * min(1.0, written / total)
                    self.on_phase(f"downloading {written}/{total} B", pct)

            path, size, digest = download_file(
                self.client,
                target,
                self.config.download_dir,
                filename=res.filename_hint,
                headers=self.headers or None,
                on_progress=_dl_prog,
            )
            self._phase("finalizing", 97.0)
            rec = self.registry.mark_completed(
                rec.id,
                file_path=str(path),
                file_size=size,
                file_hash=digest,
                content_type=res.content_type,
            )
            self._phase("done", 100.0)
            console.print(f"[green]完了[/green] {path} ({size:,} bytes)")
            return ActionResult(
                status="downloaded",
                message=str(path),
                path=path,
                record_id=rec.id,
            )
        except Exception as e:
            self.registry.mark_failed(rec.id, str(e))
            console.print(f"[red]失敗[/red] {e}")
            return ActionResult(status="failed", message=str(e), record_id=rec.id)

    # ------------------------------------------------------------------
    # m3u8
    # ------------------------------------------------------------------

    def _handle_m3u8(
        self,
        res: ClassifiedResource,
        *,
        parent_page_url: str | None,
        source_type: SourceType,
        local_path: Path | None = None,
    ) -> ActionResult:
        target = res.final_url or res.url
        canonical = res.canonical_url or normalize_url(target)

        if is_mpd_url(target) or (local_path and local_path.suffix.lower() == ".mpd"):
            msg = "MPEG-DASH (.mpd) は MVP 対象外です。HLS (.m3u8) のみ対応しています。"
            console.print(f"[red]非対応[/red] {msg}")
            return ActionResult(status="failed", message=msg)

        try:
            self._phase("parsing m3u8", 5.0)
            info = parse_m3u8(
                self.client,
                target,
                headers=self.headers or None,
                quality=self.quality,
                local_path=local_path,
            )
        except UnsupportedIndexError as e:
            console.print(f"[red]非対応[/red] {e}")
            return ActionResult(status="failed", message=str(e))
        except Exception as e:
            console.print(f"[red]m3u8 解析失敗[/red] {e}")
            return ActionResult(status="failed", message=str(e))

        if info.is_encrypted:
            console.print(
                "[yellow]警告[/yellow] 暗号化 HLS (EXT-X-KEY) を検出。"
                " MVP は非 DRM 平文を想定。ffmpeg が鍵取得できる場合のみ成功します。"
            )
        if not info.is_endlist:
            console.print(
                "[yellow]警告[/yellow] ライブ窓（#EXT-X-ENDLIST なし）の可能性。"
                " 追従録画は MVP 対象外で、現在の窓のみ取得します。"
            )

        if info.is_master and info.variants:
            console.print(f"[dim]マスタープレイリスト: {len(info.variants)} バリアント[/dim]")
            for i, v in enumerate(info.variants):
                mark = "←" if v.absolute_uri == info.media_playlist_url else " "
                console.print(f"  {mark} [{i}] {v.label()}")
            console.print(f"[dim]選択 quality={self.quality} → {info.media_playlist_url}[/dim]")
        # Registry: URL or fingerprint
        existing = self.registry.is_downloaded(canonical, fingerprint=info.fingerprint)
        if not existing:
            # also check media playlist URL
            existing = self.registry.is_downloaded(
                normalize_url(info.media_playlist_url), fingerprint=info.fingerprint
            )
        if existing and not self.force:
            path = existing.file_path or "?"
            console.print(
                f"[yellow]スキップ[/yellow] 同一ストリームは既にダウンロード済み "
                f"(id={existing.id}, path={path}, fingerprint={info.fingerprint[:12]}…)"
            )
            return ActionResult(
                status="skipped",
                message=f"既にダウンロード済み: {path}",
                path=Path(path) if existing.file_path else None,
                record_id=existing.id,
            )

        title = res.filename_hint or filename_from_url(target, default="stream")
        if title.endswith(".m3u8"):
            title = title[: -len(".m3u8")] + ".mp4"

        rec = self.registry.create(
            target,
            canonical_url=canonical,
            source_type=source_type,
            content_type=res.content_type or "application/vnd.apple.mpegurl",
            parent_page_url=parent_page_url,
            m3u8_fingerprint=info.fingerprint,
            title=title,
            meta={
                "media_playlist_url": info.media_playlist_url,
                "segment_count": info.segment_count,
                "duration_approx": info.duration_approx,
                "is_master": info.is_master,
            },
        )
        try:
            segs = info.segment_count or "?"
            self._phase(f"hls download (~{segs} segs)", 15.0)
            console.print(
                f"[cyan]HLS ダウンロード中[/cyan] segments≈{info.segment_count} "
                f"duration≈{info.duration_approx or '?'}s"
            )
            path, size = download_m3u8_ffmpeg(
                info.media_playlist_url,
                self.config.download_dir,
                filename=title if title.endswith(".mp4") else title + ".mp4",
                headers=self.headers or None,
            )
            self._phase("hashing", 95.0)
            digest = sha256_file(path)
            rec = self.registry.mark_completed(
                rec.id,
                file_path=str(path),
                file_size=size,
                file_hash=digest,
                m3u8_fingerprint=info.fingerprint,
            )
            console.print(f"[green]完了[/green] {path} ({size:,} bytes)")
            return ActionResult(
                status="downloaded",
                message=str(path),
                path=path,
                record_id=rec.id,
            )
        except Exception as e:
            self.registry.mark_failed(rec.id, str(e))
            console.print(f"[red]失敗[/red] {e}")
            return ActionResult(status="failed", message=str(e), record_id=rec.id)

    # ------------------------------------------------------------------
    # HTML scrape
    # ------------------------------------------------------------------

    def _handle_html(
        self,
        res: ClassifiedResource,
        *,
        select_all: bool,
        auto_yes: bool,
    ) -> list[ActionResult]:
        page_url = res.final_url or res.url
        try:
            r = self.client.get(
                page_url,
                headers={**self.client.headers, **self.headers},
                timeout=self.timeout,
            )
            r.raise_for_status()
            html = r.text
            page_url = str(r.url)
        except Exception as e:
            console.print(f"[red]ページ取得失敗[/red] {e}")
            return [ActionResult(status="failed", message=str(e))]

        candidates = extract_candidates(html, page_url)
        if not candidates:
            console.print("[yellow]動画・インデックス候補が見つかりませんでした。[/yellow]")
            return [ActionResult(status="listed", message="候補なし")]

        # Annotate with registry status
        rows: list[tuple[MediaCandidate, Optional[object]]] = []
        for c in candidates:
            fp = None
            existing = self.registry.is_downloaded(normalize_url(c.url))
            rows.append((c, existing))

        table = Table(title=f"メディア候補 ({len(candidates)}) — {page_url}")
        table.add_column("#", style="bold", width=4)
        table.add_column("状態", width=12)
        table.add_column("種別", width=8)
        table.add_column("ソース", width=12)
        table.add_column("URL / タイトル")

        for i, (c, existing) in enumerate(rows, start=1):
            if existing:
                status = f"[yellow]済 id={existing.id}[/yellow]"
            elif is_mpd_url(c.url):
                status = "[red]非対応[/red]"
            else:
                status = "[green]新規[/green]"
            title_bit = f"\n  {c.title}" if c.title else ""
            note = " [MPD=MVP外]" if is_mpd_url(c.url) else ""
            table.add_row(
                str(i),
                status,
                c.kind,
                c.source,
                c.url + note + title_bit,
            )
        console.print(table)

        # Selection
        def _is_actionable(c: MediaCandidate, existing: object) -> bool:
            if existing is not None:
                return False
            if is_mpd_url(c.url):
                return False
            return True

        new_indices = [
            i for i, (c, ex) in enumerate(rows) if _is_actionable(c, ex)
        ]
        if select_all:
            chosen_idx = list(range(len(rows)))
        elif auto_yes:
            # download all new only (skip MPD / already-done)
            chosen_idx = new_indices
            if not chosen_idx:
                console.print(
                    "[dim]新規の取得可能候補がないため何もダウンロードしません。[/dim]"
                )
                return [
                    ActionResult(
                        status="skipped",
                        message="新規候補なし（済または MVP 非対応のみ）",
                    )
                ]
        else:
            chosen_idx = self._prompt_selection(len(rows), new_indices)
            if chosen_idx is None:
                return [ActionResult(status="listed", message="キャンセル")]

        results: list[ActionResult] = []
        for i in chosen_idx:
            c, existing = rows[i]
            if existing and not self.force:
                console.print(
                    f"[yellow]#{i+1} スキップ[/yellow] 既にダウンロード済み "
                    f"(id={existing.id}, {existing.file_path})"
                )
                results.append(
                    ActionResult(
                        status="skipped",
                        message=f"済: {existing.file_path}",
                        path=Path(existing.file_path) if existing.file_path else None,
                        record_id=existing.id,
                    )
                )
                continue

            console.print(f"\n[bold]#{i+1} をダウンロード[/bold] {c.url}")
            # Re-enter process for this media URL
            sub = self.process_url(
                c.url,
                parent_page_url=page_url,
                source_type=SourceType.FROM_HTML,
            )
            results.extend(sub)
        return results

    def _prompt_selection(
        self, n: int, new_indices: list[int]
    ) -> list[int] | None:
        """
        Interactive selection.
        Accepts: numbers (1 2 3), ranges (1-3), 'all', 'new', 'none'/empty cancel.
        """
        console.print(
            "\n[bold]ダウンロードする番号を選択[/bold] "
            "(例: 1 3 / 1-4 / new / all / Enter=キャンセル)"
        )
        try:
            raw = console.input("[cyan]選択 > [/cyan]").strip()
        except (EOFError, KeyboardInterrupt):
            console.print("\nキャンセル")
            return None

        if not raw or raw.lower() in {"none", "q", "cancel", "n"}:
            console.print("キャンセル")
            return None
        if raw.lower() in {"all", "a", "*"}:
            return list(range(n))
        if raw.lower() in {"new", "nonly"}:
            if not new_indices:
                console.print("新規候補がありません")
                return None
            return list(new_indices)

        selected: set[int] = set()
        for token in raw.replace(",", " ").split():
            token = token.strip()
            if not token:
                continue
            if "-" in token:
                try:
                    a, b = token.split("-", 1)
                    start, end = int(a), int(b)
                    for i in range(start, end + 1):
                        if 1 <= i <= n:
                            selected.add(i - 1)
                except ValueError:
                    console.print(f"[red]無効な範囲: {token}[/red]")
                    return None
            else:
                try:
                    i = int(token)
                    if 1 <= i <= n:
                        selected.add(i - 1)
                    else:
                        console.print(f"[red]範囲外: {i}[/red]")
                        return None
                except ValueError:
                    console.print(f"[red]無効な入力: {token}[/red]")
                    return None
        if not selected:
            return None
        return sorted(selected)
