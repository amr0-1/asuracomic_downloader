#!/usr/bin/env python3
"""
Unified entry point for the Asura Scans downloader.

A single :mod:`typer` application provides both the command line and the
desktop GUI::

    python main.py search "solo swordmaster"
    python main.py chapters https://asurascans.com/comics/solo-swordmaster
    python main.py download https://asurascans.com/comics/solo-swordmaster/chapter/25
    python main.py batch https://asurascans.com/comics/solo-swordmaster --chapters 1-5
    python main.py gui

Python 3.15 notes
-----------------
* **PEP 810 - explicit lazy imports.**  Every heavy dependency
  (:mod:`core.extractor`, :mod:`core.downloader`, PyQt5) is imported lazily
  at the top of this module.  ``python main.py search`` therefore never
  loads the GUI toolkit or Pillow - only the code path that is actually
  used gets imported.
* **PEP 814 - immutable configuration.**  The frozen mappings from
  :mod:`core.config` are shared as-is; nothing mutates them.
* **PEP 686 - UTF-8 by default.**  ``open()`` is called without an
  ``encoding=`` argument.

The command functions are synchronous (Typer requires that) and bridge into
the async engine with :func:`asyncio.run`.  The GUI runs its own asyncio
work on a dedicated worker thread so the Qt event loop never blocks.
"""

from __future__ import annotations

import asyncio
import os
from typing import Any

import typer
from rich.console import Console
from rich.progress import (
    BarColumn,
    Progress,
    SpinnerColumn,
    TaskProgressColumn,
    TextColumn,
    TimeElapsedColumn,
)
from rich.table import Table

# isort: off
# PEP 810 - explicit lazy imports.  Nothing below is loaded until the
# corresponding command actually needs it.
lazy import core.extractor
lazy import core.downloader
lazy import gui.app
lazy from PyQt5.QtWidgets import QApplication

# isort: on

from utils.sanitizer import sanitize_filename

__all__ = ["app", "main"]

app = typer.Typer(
    name="asura",
    help="Asura Scans downloader - async, API-driven manga downloader.",
    no_args_is_help=True,
    add_completion=False,
)
console = Console()

#: Formats the compiler understands.
VALID_FORMATS = ("none", "pdf", "cbz")


# ============================================================
# HELPERS
# ============================================================
def _fail(message: str) -> None:
    """Print an error and abort the command."""
    console.print(f"[bold red]Error:[/] {message}")
    raise typer.Exit(code=1)


def _normalise_format(value: str | None) -> str:
    """Validate and normalise the ``--format`` option."""
    fmt = (value or "none").strip().lower()
    if fmt in {"", "none", "raw", "images"}:
        return "none"
    if fmt not in VALID_FORMATS:
        _fail(f"Invalid format {value!r}. Choose one of: {', '.join(VALID_FORMATS)}.")
    return fmt


def _parse_chapter_selection(selection: str | None) -> set[float]:
    """
    Parse a chapter selection string into a set of numbers.

    Accepts ``"1,3,5"``, ``"1-5"`` and combinations such as ``"1-3,7,10-12"``.
    """
    if not selection:
        return set()

    wanted: set[float] = set()
    for part in selection.split(","):
        part = part.strip()
        if not part:
            continue
        if "-" in part:
            start_text, _, end_text = part.partition("-")
            try:
                start = float(start_text.strip())
                end = float(end_text.strip())
            except ValueError:
                _fail(f"Invalid chapter range {part!r}.")
            if start > end:
                start, end = end, start
            # Include fractional chapters such as 10.5.
            step = 0.5 if any(
                value % 1 for value in (start, end)
            ) else 1
            count = int(round((end - start) / step)) + 1
            wanted.update(round(start + index * step, 2) for index in range(count))
        else:
            try:
                wanted.add(float(part))
            except ValueError:
                _fail(f"Invalid chapter number {part!r}.")
    return wanted


def _chapter_sort_key(number: float) -> tuple[int, float]:
    """Sort key that keeps chapter 2 before chapter 10 and 10.5 after 10."""
    return (int(number), number)


def _series_output_dir(output_dir: str, slug: str) -> str:
    """Build ``<output_dir>/<series-slug>`` and create it."""
    target = os.path.join(output_dir, sanitize_filename(slug))
    os.makedirs(target, exist_ok=True)
    return target


def _chapter_output_dir(output_dir: str, slug: str, number: float) -> str:
    """Build the per-chapter output directory."""
    label = f"chapter_{number:g}"
    target = os.path.join(output_dir, sanitize_filename(slug), sanitize_filename(label))
    os.makedirs(target, exist_ok=True)
    return target


def _match_chapter(listing: list[Any], url: str) -> Any | None:
    """
    Find the chapter in *listing* that *url* points at.

    Both ``/chapter/25`` and ``/chapter/chapter-25`` are understood.
    """
    try:
        _, number = core.extractor.parse_chapter_url(url)
    except ValueError:
        number = None
    if number is None:
        return None
    for chapter in listing:
        if chapter.number == number:
            return chapter
    return None


# ============================================================
# COMMANDS
# ============================================================
@app.command()
def search(
    query: str = typer.Argument(..., help="The title to search for."),
    pages: int = typer.Option(1, "--pages", "-p", help="How many result pages to fetch."),
    limit: int = typer.Option(20, "--limit", "-n", help="Results per page (max 50)."),
) -> None:
    """
    Search Asura Scans for a series.
    """
    console.print(f"[bold green]Searching for:[/] {query}")

    async def run() -> list[Any]:
        async with core.extractor.AsuraExtractor() as extractor:
            return await extractor.search(query, pages=pages, per_page=limit)

    try:
        results = asyncio.run(run())
    except Exception as exc:
        _fail(f"Search failed: {exc}")
        return

    if not results:
        console.print("[bold yellow]No results found.[/]")
        raise typer.Exit()

    table = Table(title=f"Results for '{query}'")
    table.add_column("#", justify="right", style="dim")
    table.add_column("Title", style="cyan", no_wrap=True)
    table.add_column("Latest", justify="right", style="magenta")
    table.add_column("Chapters", justify="right", style="dim")
    table.add_column("Slug", style="green")

    for index, result in enumerate(results, start=1):
        table.add_row(
            str(index),
            result.title,
            result.latest_chapter or "-",
            str(result.chapter_count),
            result.slug,
        )
    console.print(table)
    console.print(
        "[dim]Use a slug with the 'chapters' or 'batch' command.[/]"
    )


@app.command()
def chapters(
    url: str = typer.Argument(..., help="The series URL or slug."),
    show_premium: bool = typer.Option(
        True, "--premium/--no-premium", help="Include premium/locked chapters."
    ),
) -> None:
    """
    List the chapters available for a series.
    """
    console.print(f"[bold green]Looking up:[/] {url}")

    async def run() -> tuple[Any, list[Any]]:
        async with core.extractor.AsuraExtractor() as extractor:
            slug = core.extractor.parse_series_url(url)
            series = await extractor.get_series(slug)
            listing = await extractor.list_chapters(series.slug)
            return series, listing

    try:
        series, listing = asyncio.run(run())
    except core.extractor.SeriesNotFoundError:
        _fail(f"Series not found: {url}")
        return
    except Exception as exc:
        _fail(f"Could not list chapters: {exc}")
        return

    table = Table(title=f"{series.title} - {len(listing)} chapters")
    table.add_column("#", justify="right", style="dim")
    table.add_column("Chapter", style="cyan")
    table.add_column("Pages", justify="right", style="green")
    table.add_column("Access", style="magenta")
    table.add_column("Published", style="dim")

    for chapter in sorted(listing, key=lambda item: item.number):
        if chapter.is_premium and not show_premium:
            continue
        table.add_row(
            str(chapter.number),
            chapter.slug,
            str(chapter.page_count),
            "premium" if chapter.is_premium else "free",
            (chapter.published_at or "")[:10],
        )
    console.print(table)


@app.command()
def download(
    url: str = typer.Argument(..., help="The chapter URL to download."),
    output: str = typer.Option("downloads", "--output", "-o", help="Output directory."),
    format: str = typer.Option("pdf", "--format", "-f", help="pdf, cbz or none."),
    keep_images: bool = typer.Option(
        False, "--keep-images", help="Also keep the individual page images."
    ),
) -> None:
    """
    Download a single chapter and compile it to PDF or CBZ.
    """
    fmt = _normalise_format(format)
    console.print(f"[bold green]Downloading:[/] {url}")

    async def run() -> tuple[Any, Any]:
        async with core.extractor.AsuraExtractor() as extractor:
            slug = core.extractor.parse_series_url(url)
            series = await extractor.get_series(slug)
            chapters = await extractor.list_chapters(series.slug)
            chapter = _match_chapter(chapters, url)
            if chapter is None:
                return series, None
            extraction = await extractor.get_chapter(series.slug, chapter.slug)
            return series, (chapter, extraction)

    try:
        series, payload = asyncio.run(run())
    except Exception as exc:
        _fail(f"Download failed: {exc}")
        return

    if payload is None:
        _fail(f"Could not match that URL to a chapter of {series.title}.")
        return

    chapter, extraction = payload

    if extraction.is_skipped:
        # Skip-with-reason: a paywalled chapter is a warning, not a crash.
        console.print(f"[bold yellow]Skipped:[/] {extraction.reason}")
        raise typer.Exit()

    target = _chapter_output_dir(output, series.slug, chapter.number)

    async def fetch() -> Any:
        async with core.downloader.ChapterDownloader() as downloader:
            with Progress(
                SpinnerColumn(),
                TextColumn("[bold blue]{task.description}"),
                BarColumn(),
                TaskProgressColumn(),
                TimeElapsedColumn(),
                console=console,
                transient=True,
            ) as progress:
                task = progress.add_task(
                    f"Downloading {chapter.label}", total=len(extraction.pages)
                )
                return await downloader.download_chapter(
                    extraction,
                    target,
                    fmt=fmt,
                    save_images=keep_images,
                    progress_callback=lambda done, total: progress.update(
                        task, completed=done, total=total
                    ),
                )

    try:
        result = asyncio.run(fetch())
    except Exception as exc:
        _fail(f"Download failed: {exc}")
        return

    if not result.ok:
        _fail(result.reason or "The chapter could not be downloaded.")
        return

    console.print(
        f"[bold green]Done.[/] {result.pages} pages "
        f"({result.bytes_downloaded / 1024 / 1024:.1f} MB) in {result.duration:.1f}s"
    )
    if result.output_path:
        console.print(f"[dim]Saved to:[/] {result.output_path}")
    if result.reason:
        console.print(f"[yellow]{result.reason}[/]")


@app.command()
def batch(
    url: str = typer.Argument(..., help="The series URL or slug."),
    chapters: str = typer.Option(
        None, "--chapters", "-c", help="Chapter numbers, e.g. '1-5' or '1,3,7-9'."
    ),
    all_chapters: bool = typer.Option(False, "--all", help="Download every chapter."),
    output: str = typer.Option("downloads", "--output", "-o", help="Output directory."),
    format: str = typer.Option("pdf", "--format", "-f", help="pdf, cbz or none."),
    keep_images: bool = typer.Option(
        False, "--keep-images", help="Also keep the individual page images."
    ),
) -> None:
    """
    Download several chapters of a series in one go.
    """
    fmt = _normalise_format(format)
    if not all_chapters and not chapters:
        _fail("Specify chapters with --chapters or use --all.")
        return

    wanted = _parse_chapter_selection(chapters)
    console.print(f"[bold green]Preparing batch for:[/] {url}")

    async def run() -> tuple[Any, list[Any]]:
        async with core.extractor.AsuraExtractor() as extractor:
            slug = core.extractor.parse_series_url(url)
            series = await extractor.get_series(slug)
            listing = await extractor.list_chapters(series.slug)
            return series, listing

    try:
        series, listing = asyncio.run(run())
    except Exception as exc:
        _fail(f"Could not list chapters: {exc}")
        return

    if all_chapters:
        selected = list(listing)
    else:
        selected = [
            chapter for chapter in listing if float(chapter.number) in wanted
        ]
        selected.sort(key=lambda item: item.number, reverse=True)

    if not selected:
        _fail("None of the requested chapters were found.")
        return

    console.print(
        f"[bold cyan]{len(selected)} chapters selected[/] for {series.title}."
    )

    skipped: list[str] = []
    failed: list[str] = []
    downloaded = 0
    pages_total = 0

    # The extraction and the download share one event loop so that the
    # aiohttp connection pool is reused across chapters.
    async def process_chapters() -> None:
        nonlocal downloaded, pages_total
        async with core.extractor.AsuraExtractor() as extractor:
            async with core.downloader.ChapterDownloader() as downloader:
                with Progress(
                    SpinnerColumn(),
                    TextColumn("[bold blue]{task.description}"),
                    BarColumn(),
                    TaskProgressColumn(),
                    TimeElapsedColumn(),
                    console=console,
                ) as progress:
                    task = progress.add_task(
                        "Downloading", total=len(selected)
                    )
                    for chapter in selected:
                        target = _chapter_output_dir(
                            output, series.slug, chapter.number
                        )
                        extraction = await extractor.get_chapter(
                            series.slug, chapter.slug, fallback=chapter
                        )
                        if extraction.is_skipped:
                            skipped.append(f"ch{chapter.number}: {extraction.reason}")
                            progress.update(task, advance=1)
                            continue
                        result = await downloader.download_chapter(
                            extraction,
                            target,
                            fmt=fmt,
                            save_images=keep_images,
                        )
                        if result.ok:
                            downloaded += 1
                            pages_total += result.pages
                        else:
                            failed.append(f"ch{chapter.number}: {result.reason}")
                        progress.update(task, advance=1)

    try:
        asyncio.run(process_chapters())
    except Exception as exc:
        _fail(f"Batch failed: {exc}")
        return

    console.print(
        f"[bold green]Batch complete.[/] {downloaded} chapters, "
        f"{pages_total} pages."
    )
    for note in skipped:
        console.print(f"[bold yellow]Skipped[/] {note}")
    for note in failed:
        console.print(f"[bold red]Failed[/] {note}")


@app.command()
def gui() -> None:
    """
    Launch the PyQt5 desktop application.
    """
    # Both the PyQt5 import and the GUI module are resolved lazily, so
    # the rest of the CLI never pays for them.
    try:
        qt_app = QApplication.instance() or QApplication([])
    except ImportError as exc:
        _fail(
            f"PyQt5 is not installed ({exc}). "
            "Install it with: pip install 'asuracomic-downloader[gui]'"
        )
        return

    gui.app.run(qt_app)


# ============================================================
# ENTRY POINT
# ============================================================
def main() -> None:
    """Console-script entry point."""
    app()


if __name__ == "__main__":
    main()
