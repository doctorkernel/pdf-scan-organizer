# PDF Scan Organizer

Local-first organizer for scanned PDFs already sitting in a folder tree.

Behavior:

- recursively walks an input root
- only processes files whose names still look like unrenamed scans
- default pattern: `scan####.pdf`, `scana####.pdf`, `scanb####.pdf`
- batches local PDF text to LM Studio for better naming
- prefers page 1 as the primary naming/classification source
- writes into `YYYY/MM-Mon`
- names files as `YYYY-MM-DD Description-Codex.pdf`
- appends `-Codex-ToReview` when later pages appear unrelated to page 1
- appends `-OCRMissing` when the file still has too little text to trust naming
- can run local OCR with `ocrmypdf` for low-signal scans
- when OCR is used, saves the OCR-enhanced PDF as the main renamed file and also writes a sibling `...[Original].pdf`
- keeps a resume manifest so you can stop and restart safely
- estimates LM Studio input tokens and automatically splits oversized batches
- can distribute one-document LM Studio requests across multiple hosts

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
- `dispatch_window` is how many PDFs the organizer accumulates before handing work to the multi-host dispatcher
- `max_input_tokens` is the estimated input-token budget that can force smaller batches
- `lm_studio.endpoints` can list 1, 2, or 3 LM Studio hosts; the organizer will round-robin work across them
- the safest anti-contamination setting is `batch_size = 1`
- a good conservative setting is `batch_size = 1`, `dispatch_window = 3`, and `max_input_tokens = 4000`

OCR settings:

- `ocr.enabled = true` turns on `ocrmypdf` for low-signal scans
- `ocr.command` controls which OCR binary to invoke
- `ocr.preserve_original = true` keeps a `...[Original].pdf` beside the renamed OCR output

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
