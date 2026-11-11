# gui/app.py
"""
PyQt5 desktop application for the Asura Scans downloader.

The GUI is a thin shell around the same async engine the CLI uses.  Two
details are worth calling out:

* **PEP 810 - lazy imports.**  PyQt5, the extractor and the downloader are
  all imported lazily, so importing this module does not pull the GUI
  toolkit into a CLI process.
* **The event loop never blocks.**  All async work runs on a dedicated
  :class:`QThread` worker that owns its own :func:`asyncio.run` loop.
  Results travel back to the UI thread through Qt signals, which are
  thread-safe.  The user can therefore search, browse chapters and cancel a
  running batch without the window ever freezing.
"""

from __future__ import annotations

import asyncio
import os
import sys
from typing import Any

# isort: off
# PEP 810 - explicit lazy imports.
lazy import core.extractor
lazy import core.downloader

lazy from PyQt5.QtCore import QThread, Qt, pyqtSignal
lazy from PyQt5.QtGui import QBrush, QPalette, QPixmap
lazy from PyQt5.QtWidgets import (
    QApplication,
    QCheckBox,
    QComboBox,
    QFileDialog,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QMainWindow,
    QMessageBox,
    QProgressBar,
    QPushButton,
    QTextEdit,
    QVBoxLayout,
    QWidget,
)

# isort: on

from utils.sanitizer import sanitize_filename

__all__ = ["run", "AsuraWindow"]


# ============================================================
# WORKER THREAD
# ============================================================
class _Worker(QThread):
    """
    Runs one async job on its own event loop.

    The job is a coroutine *factory* so that the coroutine is created
    inside the worker thread, where its event loop lives.
    """

    progress = pyqtSignal(int, int)
    message = pyqtSignal(str)
    finished_result = pyqtSignal(object)

    def __init__(self, factory: Any, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._factory = factory
        self._stop_requested = False

    def request_stop(self) -> None:
        """Ask the running job to stop at the next checkpoint."""
        self._stop_requested = True

    @property
    def stop_requested(self) -> bool:
        return self._stop_requested

    def run(self) -> None:  # noqa: D102 - Qt override
        try:
            result = asyncio.run(self._factory(self))
        except Exception as exc:  # pragma: no cover - defensive
            self.finished_result.emit({"error": f"{type(exc).__name__}: {exc}"})
            return
        self.finished_result.emit(result)


# ============================================================
# MAIN WINDOW
# ============================================================
class AsuraWindow(QMainWindow):
    """The main application window."""

    def __init__(self) -> None:
        super().__init__()
        self.setWindowTitle("Asura Scans Downloader")
        self.setGeometry(110, 110, 860, 680)

        self._worker: _Worker | None = None
        self._chapters: list[Any] = []

        self.central = QWidget(self)
        self.setCentralWidget(self.central)
        self.layout = QVBoxLayout(self.central)

        self._build_ui()
        self._apply_styles()

    # ------------------------------------------------------------------
    # UI construction
    # ------------------------------------------------------------------
    def _build_ui(self) -> None:
        self.layout.addWidget(self._build_search_box())
        self.layout.addWidget(self._build_download_box())
        self.layout.addWidget(self._build_output_box())

        self.status_display = QTextEdit()
        self.status_display.setReadOnly(True)
        self.layout.addWidget(self.status_display)

    def _build_search_box(self) -> QGroupBox:
        box = QGroupBox("Search Manga")
        layout = QVBoxLayout(box)

        row = QHBoxLayout()
        row.addWidget(QLabel("Title:"))
        self.query_input = QLineEdit()
        self.query_input.setPlaceholderText("e.g. solo swordmaster")
        self.query_input.returnPressed.connect(self.perform_search)
        row.addWidget(self.query_input)

        row.addWidget(QLabel("Pages:"))
        self.pages_input = QLineEdit("1")
        self.pages_input.setMaximumWidth(60)
        row.addWidget(self.pages_input)

        self.search_button = QPushButton("Search")
        self.search_button.clicked.connect(self.perform_search)
        row.addWidget(self.search_button)
        layout.addLayout(row)

        self.results_list = QListWidget()
        self.results_list.itemClicked.connect(self._on_result_clicked)
        layout.addWidget(self.results_list)
        return box

    def _build_download_box(self) -> QGroupBox:
        box = QGroupBox("Download Chapters")
        layout = QVBoxLayout(box)

        row = QHBoxLayout()
        row.addWidget(QLabel("Series URL or slug:"))
        self.url_input = QLineEdit()
        self.url_input.setPlaceholderText("https://asurascans.com/comics/<slug>")
        row.addWidget(self.url_input)
        layout.addLayout(row)

        buttons = QHBoxLayout()
        self.list_button = QPushButton("Load Chapters")
        self.list_button.clicked.connect(self.perform_list_chapters)
        buttons.addWidget(self.list_button)

        self.select_all_button = QPushButton("Select All")
        self.select_all_button.clicked.connect(lambda: self._set_all_checked(True))
        buttons.addWidget(self.select_all_button)

        self.select_none_button = QPushButton("Select None")
        self.select_none_button.clicked.connect(lambda: self._set_all_checked(False))
        buttons.addWidget(self.select_none_button)
        layout.addLayout(buttons)

        self.chapters_list = QListWidget()
        layout.addWidget(self.chapters_list)

        action_row = QHBoxLayout()
        self.download_button = QPushButton("Download Selected")
        self.download_button.clicked.connect(self.perform_download)
        action_row.addWidget(self.download_button)

        self.stop_button = QPushButton("Stop")
        self.stop_button.clicked.connect(self.perform_stop)
        self.stop_button.setEnabled(False)
        action_row.addWidget(self.stop_button)

        self.progress_bar = QProgressBar()
        self.progress_bar.setValue(0)
        action_row.addWidget(self.progress_bar)
        layout.addLayout(action_row)
        return box

    def _build_output_box(self) -> QGroupBox:
        box = QGroupBox("Output")
        layout = QVBoxLayout(box)

        row = QHBoxLayout()
        row.addWidget(QLabel("Directory:"))
        self.output_input = QLineEdit("downloads")
        row.addWidget(self.output_input)

        browse = QPushButton("Browse")
        browse.clicked.connect(self._browse_output)
        row.addWidget(browse)
        layout.addLayout(row)

        options = QHBoxLayout()
        options.addWidget(QLabel("Format:"))
        self.format_combo = QComboBox()
        for label in ("pdf", "cbz", "none (images only)"):
            self.format_combo.addItem(label)
        self.format_combo.setCurrentIndex(0)
        options.addWidget(self.format_combo)

        self.keep_images_check = QCheckBox("Also keep page images")
        options.addWidget(self.keep_images_check)
        layout.addLayout(options)
        return box

    def _apply_styles(self) -> None:
        background = os.path.join(
            os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
            "assets",
            "background.png",
        )
        try:
            pixmap = QPixmap(background)
            if not pixmap.isNull():
                palette = self.palette()
                palette.setBrush(QPalette.Window, QBrush(pixmap))
                self.setPalette(palette)
                self.setAutoFillBackground(True)
        except Exception:
            # A missing background is purely cosmetic.
            pass

        self.setStyleSheet(
            """
            QPushButton {
                background-color: rgba(255, 255, 255, 50);
                border: 1px solid rgba(255, 255, 255, 100);
                border-radius: 5px;
                padding: 8px 16px;
                color: white;
            }
            QPushButton:hover { background-color: rgba(255, 255, 255, 80); }
            QPushButton:disabled { background-color: rgba(255, 255, 255, 20); color: rgba(255,255,255,120); }
            QLineEdit, QTextEdit {
                background-color: rgba(255, 255, 255, 50);
                border: 1px solid rgba(255, 255, 255, 100);
                border-radius: 5px;
                padding: 5px;
                color: white;
            }
            QLabel { color: white; }
            QGroupBox { color: white; border: 1px solid rgba(255,255,255,80); border-radius: 5px; }
            QListWidget {
                background-color: rgba(255, 255, 255, 50);
                border: 1px solid rgba(255, 255, 255, 100);
                border-radius: 5px;
                padding: 5px;
                color: white;
            }
            QComboBox {
                background-color: rgba(255, 255, 255, 50);
                border: 1px solid rgba(255, 255, 255, 100);
                border-radius: 5px;
                padding: 5px;
                color: white;
            }
            QComboBox QAbstractItemView {
                background-color: rgba(40, 40, 40, 220);
                color: white;
                selection-background-color: rgba(0, 255, 0, 120);
            }
            QCheckBox { color: white; }
            QProgressBar {
                border: 1px solid rgba(255, 255, 255, 100);
                border-radius: 5px;
                text-align: center;
                color: white;
            }
            QProgressBar::chunk { background-color: rgba(0, 255, 0, 150); }
            """
        )

    # ------------------------------------------------------------------
    # Small helpers
    # ------------------------------------------------------------------
    def _log(self, text: str) -> None:
        self.status_display.append(text)

    def _set_all_checked(self, checked: bool) -> None:
        state = Qt.Checked if checked else Qt.Unchecked
        for index in range(self.chapters_list.count()):
            item = self.chapters_list.item(index)
            item.setCheckState(state)

    def _browse_output(self) -> None:
        chosen = QFileDialog.getExistingDirectory(self, "Select Output Directory")
        if chosen:
            self.output_input.setText(chosen)

    def _on_result_clicked(self, item: QListWidgetItem) -> None:
        payload = item.data(Qt.UserRole)
        if payload:
            self.url_input.setText(payload["slug"])

    def _set_busy(self, busy: bool) -> None:
        self.search_button.setEnabled(not busy)
        self.list_button.setEnabled(not busy)
        self.download_button.setEnabled(not busy)
        self.stop_button.setEnabled(busy)

    def _start_worker(self, factory: Any, on_done: Any) -> None:
        """Run *factory* on a worker thread, wiring up the signals."""
        worker = _Worker(factory)
        worker.message.connect(self._log)
        worker.finished_result.connect(on_done)
        self._worker = worker
        self._set_busy(True)
        worker.start()

    # ------------------------------------------------------------------
    # Search
    # ------------------------------------------------------------------
    def perform_search(self) -> None:
        query = self.query_input.text().strip()
        if not query:
            QMessageBox.warning(self, "Search", "Please enter a search term.")
            return
        try:
            pages = max(1, int(self.pages_input.text() or "1"))
        except ValueError:
            pages = 1

        self.results_list.clear()
        self._log(f"Searching for '{query}'...")

        async def job(worker: _Worker) -> dict[str, Any]:
            async with core.extractor.AsuraExtractor() as extractor:
                worker.message.emit(f"Searching for '{query}'...")
                results = await extractor.search(query, pages=pages, per_page=20)
            return {"results": results}

        def done(payload: dict[str, Any]) -> None:
            self._set_busy(False)
            if "error" in payload:
                self._log(f"Search failed: {payload['error']}")
                return
            for result in payload.get("results", []):
                item = QListWidgetItem(f"{result.title}  ({result.latest_chapter})")
                item.setData(Qt.UserRole, {"slug": result.slug, "title": result.title})
                self.results_list.addItem(item)
            self._log(f"Found {len(payload.get('results', []))} results.")

        self._start_worker(job, done)

    # ------------------------------------------------------------------
    # Chapter listing
    # ------------------------------------------------------------------
    def perform_list_chapters(self) -> None:
        url = self.url_input.text().strip()
        if not url:
            QMessageBox.warning(self, "Chapters", "Please enter a series URL or slug.")
            return

        self.chapters_list.clear()
        self._chapters = []

        async def job(worker: _Worker) -> dict[str, Any]:
            async with core.extractor.AsuraExtractor() as extractor:
                slug = core.extractor.parse_series_url(url)
                series = await extractor.get_series(slug)
                worker.message.emit(f"Loading chapters for {series.title}...")
                chapters = await extractor.list_chapters(series.slug)
            return {"series": series, "chapters": chapters}

        def done(payload: dict[str, Any]) -> None:
            self._set_busy(False)
            if "error" in payload:
                self._log(f"Could not load chapters: {payload['error']}")
                return
            series = payload["series"]
            self._chapters = payload["chapters"]
            for chapter in sorted(self._chapters, key=lambda item: item.number, reverse=True):
                flag = " [premium]" if chapter.is_premium else ""
                item = QListWidgetItem(
                    f"Chapter {chapter.number}  ({chapter.page_count} pages){flag}"
                )
                item.setData(Qt.UserRole, chapter)
                item.setFlags(item.flags() | Qt.ItemIsUserCheckable)
                item.setCheckState(Qt.Unchecked)
                self.chapters_list.addItem(item)
            self._log(
                f"Loaded {len(self._chapters)} chapters for {series.title} "
                f"({series.genres and ', '.join(series.genres) or 'no genres'})."
            )

        self._start_worker(job, done)

    # ------------------------------------------------------------------
    # Download
    # ------------------------------------------------------------------
    def perform_download(self) -> None:
        if not self._chapters:
            QMessageBox.warning(self, "Download", "Load the chapters first.")
            return

        selected = []
        for index in range(self.chapters_list.count()):
            item = self.chapters_list.item(index)
            if item.checkState() == Qt.Checked:
                selected.append(item.data(Qt.UserRole))
        if not selected:
            QMessageBox.warning(self, "Download", "Select at least one chapter.")
            return

        output_root = self.output_input.text().strip() or "downloads"
        fmt = self.format_combo.currentText()
        fmt = "none" if fmt.startswith("none") else fmt
        keep_images = self.keep_images_check.isChecked()
        slug = selected[0].series_slug or ""

        async def job(worker: _Worker) -> dict[str, Any]:
            results = {"ok": 0, "skipped": [], "failed": [], "pages": 0}
            async with core.extractor.AsuraExtractor() as extractor:
                async with core.downloader.ChapterDownloader() as downloader:
                    for index, chapter in enumerate(selected, start=1):
                        if worker.stop_requested:
                            worker.message.emit("Stop requested.")
                            break
                        target = os.path.join(
                            output_root,
                            sanitize_filename(chapter.series_slug or slug),
                            sanitize_filename(f"chapter_{chapter.number}"),
                        )
                        os.makedirs(target, exist_ok=True)

                        extraction = await extractor.get_chapter(
                            chapter.series_slug or slug, chapter.slug, fallback=chapter
                        )
                        if extraction.is_skipped:
                            # Skip-with-reason: log it and keep going.
                            note = f"Chapter {chapter.number}: {extraction.reason}"
                            results["skipped"].append(note)
                            worker.message.emit(f"Skipped {note}")
                            continue

                        worker.message.emit(f"Downloading chapter {chapter.number}...")
                        result = await downloader.download_chapter(
                            extraction,
                            target,
                            fmt=fmt,
                            save_images=keep_images,
                            stop_check=lambda: worker.stop_requested,
                        )
                        if result.ok:
                            results["ok"] += 1
                            results["pages"] += result.pages
                        else:
                            results["failed"].append(
                                f"Chapter {chapter.number}: {result.reason}"
                            )
                        worker.progress.emit(index, len(selected))
            return results

        def done(payload: dict[str, Any]) -> None:
            self._set_busy(False)
            if "error" in payload:
                self._log(f"Download failed: {payload['error']}")
                return
            self._log(
                f"Finished: {payload['ok']} chapters, {payload['pages']} pages."
            )
            for note in payload.get("skipped", []):
                self._log(f"Skipped {note}")
            for note in payload.get("failed", []):
                self._log(f"Failed {note}")

        self._start_worker(job, done)

    def perform_stop(self) -> None:
        if self._worker is not None:
            self._worker.request_stop()
            self._log("Stop requested - finishing the current chapter...")

    def closeEvent(self, event: Any) -> None:  # noqa: N802 - Qt override
        if self._worker is not None and self._worker.isRunning():
            self._worker.request_stop()
            self._worker.wait(5000)
        super().closeEvent(event)


# ============================================================
# ENTRY POINT
# ============================================================
def run(app: Any = None) -> None:
    """
    Start the Qt application.

    Args:
        app: an existing :class:`QApplication` to reuse.  When omitted a
            new one is created.
    """
    if app is None:
        app = QApplication.instance()
    if app is None:
        app = QApplication(sys.argv)
    window = AsuraWindow()
    window.show()
    app.exec_()


if __name__ == "__main__":
    run()
