# PDF Scan Organizer

Local-first organizer for scanned PDFs already sitting in a folder tree.

Behavior:

- recursively walks an input root
- only processes files whose names still look like unrenamed scans
- default pattern: `scan####.pdf`, `scana####.pdf`, `scanb####.pdf`
- batches local PDF text to LM Studio for better naming
- prefers page 1 as the primary naming/classification source
- writes into `YYYY/MM-Month`
- names files as `YYYY-MM-DD Description-Codex.pdf`
- appends `-Codex-ToReview` when later pages appear unrelated to page 1
- keeps a resume manifest so you can stop and restart safely
- estimates LM Studio input tokens and automatically splits oversized batches

## Setup

```bash
python3 -m venv .venv
.venv/bin/pip install -e .
cp config.example.toml config.toml
```

Edit [config.toml](/Users/openkernel/Documents/Codex/2026-04-28-what-would-be-the-best-way/pdf-scan-organizer/config.example.toml:1) for:

- `input.root_dir`
- `output.directory`
- `output.state_file`
- `output.mode`
- LM Studio settings

For large OCR-heavy scans:

- `batch_size` is the maximum number of PDFs per request
- `max_input_tokens` is the estimated input-token budget that can force smaller batches
- a good conservative setting is `batch_size = 2` and `max_input_tokens = 4000`

## Run

```bash
.venv/bin/python pdf_scan_organizer.py --config config.toml
```

Dry run:

```bash
.venv/bin/python pdf_scan_organizer.py --config config.toml --dry-run
```

The default move mode relocates matching scan files into the organized output tree.
Use `output.mode = "copy"` if you want to preserve the source scan files.
