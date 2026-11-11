# AsuraComic Downloader

## Introduction

AsuraComic Downloader downloads manga from Asura Scans. It ships a
command-line interface for automation and a PyQt5 desktop GUI for
interactive use, both built on the same asynchronous engine.

Version 4 is a ground-up rewrite. It talks to the site's **JSON API**
directly instead of driving a browser, so there is no Chromium download, no
DOM scraping and no scrolling - just fast, direct requests.

## Features

### Command-Line Interface

*   **Search** - find series by title, with configurable result pages.
*   **Chapters** - list every chapter of a series, flagging paywalled ones.
*   **Download** - fetch one chapter and compile it to PDF or CBZ.
*   **Batch** - download many chapters at once (`1-5`, `1,3,7-9`, or `--all`).
*   **In-memory compilation** - pages are held in RAM and streamed straight
    into the PDF/CBZ; the images are never written to disk first.
*   **Paywall-aware** - a premium chapter is reported and skipped, and the
    batch keeps going.

### Graphical User Interface

*   **Search** with a clickable result list that fills in the series field.
*   **Chapter browser** with per-chapter checkboxes, select-all / select-none.
*   **Output options** - directory, PDF / CBZ / raw images, keep-images toggle.
*   **Non-blocking** - all network and compilation work happens off the Qt
    thread, so the window stays responsive and the batch can be stopped.

## Installation

1.  **Clone the repository:**
    ```bash
    git clone https://github.com/Yui007/asuracomic_downloader.git
    cd asuracomic_downloader
    ```

2.  **Create a virtual environment (recommended):**
    ```bash
    python -m venv .venv

    # Windows
    .venv\Scripts\activate

    # Linux/Mac
    source .venv/bin/activate
    ```

3.  **Install dependencies:**
    ```bash
    pip install -r requirements.txt
    ```
    Python **3.15 or newer** is required - the project uses PEP 810 (lazy
    imports), PEP 814 (`frozendict`), PEP 798 (unpacking in comprehensions)
    and PEP 686 (UTF-8 by default).

4.  **Optional extras:**
    ```bash
    pip install "asuracomic-downloader[gui]"      # PyQt5 desktop GUI
    pip install "asuracomic-downloader[brotli]"   # brotli content-encoding
    ```

## Usage

### GUI

```bash
python main.py gui
```

![GUI Screenshot](gui/GUI.PNG)

1.  Search for a series and click a result to fill in the series field.
2.  Click **Load Chapters** to list the available chapters.
3.  Tick the chapters you want (or use **Select All** / **Select None**).
4.  Choose an output directory and a format.
5.  Click **Download Selected**. **Stop** cancels the run.

### CLI

```bash
# Search
python main.py search "solo swordmaster" --pages 2

# List chapters
python main.py chapters https://asurascans.com/comics/solo-swordmaster

# Download one chapter as PDF
python main.py download https://asurascans.com/comics/solo-swordmaster/chapter/25 \
    --output my_downloads --format pdf

# Batch download
python main.py batch solo-swordmaster --chapters "1-5" --format cbz
python main.py batch solo-swordmaster --chapters "1,3,7-9"
python main.py batch solo-swordmaster --all --format pdf
```

Run `python main.py <command> --help` for the full option list, or see
[CLI_usage.md](CLI_usage.md).

## How it works

*   **Dynamic domain resolution** - Asura rotates its TLD frequently, so
    nothing is hard-coded. On boot the resolver probes several candidate
    domains concurrently, verifies the API's `/health` endpoint and caches
    the result for six hours.
*   **Data-layer extraction** - series metadata, the chapter list and the
    page image URLs all come from JSON endpoints. No browser, no DOM.
*   **Token buckets** - the API and the image CDN get separate rate
    limiters, so a metadata-heavy run never throttles image throughput.
*   **Rotating fingerprints** - the `User-Agent` / `Sec-CH-UA` pair rotates
    per request. Cloudflare challenges any request without a browser
    `User-Agent`.
*   **In-memory compilation** - page bytes are decoded and written into the
    PDF/CBZ in RAM, then saved once. Pillow runs in a worker thread so the
    event loop never stalls.

## Technologies and Dependencies

| Package | Purpose | Version |
| --- | --- | --- |
| Python | Core language (PEP 810/814/798/686) | 3.15+ |
| Typer | CLI framework | >=0.9.0 |
| Rich | CLI output | >=13.7.0 |
| aiohttp | Async HTTP client | >=3.9.0 |
| Pillow | PDF compilation | >=10.0.0 |
| PyQt5 | Desktop GUI (optional) | >=5.15.0 |
| brotli | Faster CDN transfers (optional) | >=1.1.0 |

## Contributing

Contributions are welcome. If you have suggestions for improvements, bug
reports, or want to contribute code:

*   Open an issue for bugs or suggestions.
*   Submit a pull request on the
    [GitHub repository](https://github.com/Yui007/asuracomic_downloader).

## License

This project is licensed under the MIT License - see the [LICENSE](LICENSE)
file for details.
