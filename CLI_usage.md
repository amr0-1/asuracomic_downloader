# AsuraComic Downloader - CLI Usage

Everything is driven from the single root entry point, `main.py`:

```bash
python main.py <command> [OPTIONS]
```

> **Requirements:** Python 3.15+, then `pip install -r requirements.txt`.

## Commands

| Command | Purpose |
| --- | --- |
| `search` | Search Asura Scans for a series |
| `chapters` | List the chapters available for a series |
| `download` | Download a single chapter and compile it |
| `batch` | Download several chapters in one run |
| `gui` | Launch the PyQt5 desktop application |

---

### `search`

Search for a series by title.

**Usage:**

```bash
python main.py search [OPTIONS] QUERY
```

**Arguments:**

* `QUERY` - the title to search for. (Required)

**Options:**

* `--pages, -p` - how many result pages to fetch. (Default: `1`)
* `--limit, -n` - results per page, up to 50. (Default: `20`)

**Example:**

```bash
python main.py search "solo swordmaster" --pages 2
```

---

### `chapters`

List every chapter of a series. Accepts a full URL or a bare slug.

**Usage:**

```bash
python main.py chapters [OPTIONS] URL
```

**Arguments:**

* `URL` - the series URL or slug. (Required)

**Options:**

* `--premium / --no-premium` - include or hide paywalled chapters. (Default: include)

**Example:**

```bash
python main.py chapters https://asurascans.com/comics/solo-swordmaster
python main.py chapters solo-swordmaster --no-premium
```

---

### `download`

Download a single chapter and compile it to PDF or CBZ.

**Usage:**

```bash
python main.py download [OPTIONS] URL
```

**Arguments:**

* `URL` - the chapter URL. (Required)

**Options:**

* `--output, -o` - output directory. (Default: `downloads`)
* `--format, -f` - `pdf`, `cbz` or `none` (raw images only). (Default: `pdf`)
* `--keep-images` - also keep the individual page images alongside the output.

**Example:**

```bash
python main.py download https://asurascans.com/comics/solo-swordmaster/chapter/25 \
    --output my_downloads --format pdf
```

If the chapter is paywalled, the command prints a warning, writes nothing
and exits successfully - a locked chapter never aborts the run.

---

### `batch`

Download several chapters of a series.

**Usage:**

```bash
python main.py batch [OPTIONS] URL
```

**Arguments:**

* `URL` - the series URL or slug. (Required)

**Options:**

* `--chapters, -c` - chapter numbers. Accepts `1-5`, `1,3,5` and
  combinations such as `1-3,7,10-12`.
* `--all` - download every chapter.
* `--output, -o` - output directory. (Default: `downloads`)
* `--format, -f` - `pdf`, `cbz` or `none`. (Default: `pdf`)
* `--keep-images` - also keep the individual page images.

**Examples:**

```bash
# A range
python main.py batch solo-swordmaster --chapters "1-5" --format cbz

# A specific list
python main.py batch solo-swordmaster --chapters "1,3,5"

# Mixed
python main.py batch solo-swordmaster --chapters "1-3,7,10-12"

# Everything
python main.py batch solo-swordmaster --all --format pdf
```

Either `--chapters` or `--all` is required.

---

### `gui`

Launch the desktop application.

**Usage:**

```bash
python main.py gui
```

Requires PyQt5 (`pip install "asuracomic-downloader[gui]"`).

---

## Output layout

Files are written to:

```
<output>/
└── <series-slug>/
    └── chapter_<number>/
        ├── chapter-<number>.pdf     # or .cbz
        └── images/                  # only with --keep-images
            ├── 001.webp
            ├── 002.webp
            └── ...
```

Pages keep their **true** extension (`.webp`) - they are never renamed to
`.jpg`.

## Notes

* Asura rotates its domain frequently. The active host is resolved
  automatically at start-up, so any working URL is accepted.
* A paywalled chapter is reported as skipped and the batch continues with
  the remaining chapters.
* Compilation happens in memory; the individual page images are never
  written to disk unless `--keep-images` is given.
