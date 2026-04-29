#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import re
import shutil
import subprocess
import sys
import tempfile
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

from pdf_archive_core import (
    DocumentInput,
    LMStudioConfig,
    analyze_documents_batch,
    build_relative_output_path,
    extract_pdf_page_texts,
    is_low_signal_document,
    unique_output_path,
)

try:
    import tomllib
except ModuleNotFoundError:  # pragma: no cover
    tomllib = None


DEFAULT_PATTERN = r"(?i)^scan[a-z]?\d+\.pdf$"


@dataclass
class RuntimeConfig:
    input_root: Path
    filename_pattern: re.Pattern[str]
    output_dir: Path
    state_file: Path
    mode: str
    dry_run: bool
    limit: int
    lm_config: Optional[LMStudioConfig]
    ocr_enabled: bool
    ocr_command: str
    preserve_original_on_ocr: bool


@dataclass
class PendingScan:
    source_path: Path
    document_input: DocumentInput
    stat_size: int
    stat_mtime_ns: int
    original_bytes: bytes
    uses_ocr: bool = False
    ocr_missing: bool = False


def debug(message: str) -> None:
    print(f"[debug] {message}", file=sys.stderr, flush=True)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Organize scan*.pdf files into dated folders")
    parser.add_argument("--config", default="config.toml", help="Path to TOML config")
    parser.add_argument("--input-root", help="Override input root directory")
    parser.add_argument("--output-dir", help="Override output directory")
    parser.add_argument("--state-file", help="Override manifest file path")
    parser.add_argument("--limit", type=int, help="Max number of matching files to process this run")
    parser.add_argument("--dry-run", action="store_true", help="Preview actions without copying or moving files")
    return parser.parse_args()


def load_toml_config(path: Path) -> dict[str, Any]:
    debug(f"Loading config file: {path} exists={path.exists()}")
    if not path.exists():
        return {}
    if tomllib is None:
        raise RuntimeError("tomllib is unavailable; use Python 3.11+")
    with path.open("rb") as handle:
        return tomllib.load(handle)


def build_runtime_config(args: argparse.Namespace) -> RuntimeConfig:
    config_path = Path(args.config)
    raw = load_toml_config(config_path)
    debug(f"Resolved config path: {config_path}")

    input_cfg = raw.get("input", {})
    output_cfg = raw.get("output", {})
    scan_cfg = raw.get("scanning", {})
    lm_cfg = raw.get("lm_studio", {})
    ocr_cfg = raw.get("ocr", {})

    input_root = Path(args.input_root or input_cfg.get("root_dir", "."))
    filename_pattern = re.compile(str(input_cfg.get("filename_pattern", DEFAULT_PATTERN)))
    output_dir = Path(args.output_dir or output_cfg.get("directory", "organized"))
    state_file = Path(args.state_file or output_cfg.get("state_file", "state/manifest.json"))
    mode = str(output_cfg.get("mode", "move")).strip().lower()
    limit = int(args.limit or scan_cfg.get("limit", 100))

    lm_config = None
    if bool(lm_cfg.get("enabled", False)):
        lm_config = LMStudioConfig(
            base_url=str(lm_cfg.get("base_url", "http://127.0.0.1:1234")),
            model=str(lm_cfg.get("model", "")),
            batch_size=max(1, int(lm_cfg.get("batch_size", 5))),
            max_input_tokens=max(1000, int(lm_cfg.get("max_input_tokens", 6000))),
            debug=bool(lm_cfg.get("debug", False)),
        )

    return RuntimeConfig(
        input_root=input_root,
        filename_pattern=filename_pattern,
        output_dir=output_dir,
        state_file=state_file,
        mode=mode,
        dry_run=args.dry_run,
        limit=limit,
        lm_config=lm_config,
        ocr_enabled=bool(ocr_cfg.get("enabled", False)),
        ocr_command=str(ocr_cfg.get("command", "ocrmypdf")),
        preserve_original_on_ocr=bool(ocr_cfg.get("preserve_original", True)),
    )


def default_manifest() -> dict[str, Any]:
    return {"processed_files": {}, "files": [], "updated_at": None}


def load_manifest(path: Path) -> dict[str, Any]:
    if not path.exists():
        return default_manifest()
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return default_manifest()
    payload.setdefault("processed_files", {})
    payload.setdefault("files", [])
    payload.setdefault("updated_at", None)
    return payload


def save_manifest(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")


def source_signature(path: Path) -> tuple[str, int, int]:
    stat = path.stat()
    return str(path.resolve()), stat.st_size, stat.st_mtime_ns


def has_been_processed(manifest: dict[str, Any], path: Path) -> bool:
    source_path, size, mtime_ns = source_signature(path)
    record = manifest.get("processed_files", {}).get(source_path)
    return bool(record and record.get("size") == size and record.get("mtime_ns") == mtime_ns)


def iter_matching_files(config: RuntimeConfig, manifest: dict[str, Any]) -> list[Path]:
    matches: list[Path] = []
    for path in config.input_root.rglob("*.pdf"):
        if not path.is_file():
            continue
        if not config.filename_pattern.match(path.name):
            continue
        if has_been_processed(manifest, path):
            continue
        matches.append(path)
        if config.limit and len(matches) >= config.limit:
            break
    return matches


def try_ocr_pdf(source_path: Path, command: str) -> Optional[bytes]:
    if shutil.which(command) is None:
        debug(f"OCR command not found: {command}")
        return None
    with tempfile.TemporaryDirectory(prefix="pdf-scan-organizer-ocr-") as tmpdir:
        output_path = Path(tmpdir) / "ocr-output.pdf"
        cmd = [
            command,
            "--skip-text",
            "--output-type",
            "pdf",
            str(source_path),
            str(output_path),
        ]
        debug(f"Running OCR command: {' '.join(cmd)}")
        result = subprocess.run(cmd, capture_output=True, text=True)
        if result.returncode != 0:
            debug(f"OCR command failed code={result.returncode} stderr={result.stderr[:2000]}")
            return None
        return output_path.read_bytes()


def build_pending_scan(path: Path, config: RuntimeConfig) -> PendingScan:
    stat = path.stat()
    original_bytes = path.read_bytes()
    pdf_bytes = original_bytes
    page_texts = extract_pdf_page_texts(pdf_bytes)
    document_input = DocumentInput(
        source_name=path.name,
        pdf_bytes=pdf_bytes,
        fallback_date=datetime.fromtimestamp(stat.st_mtime, tz=timezone.utc),
        page_texts=page_texts,
        extracted_text="\n".join(page_texts).strip(),
    )

    uses_ocr = False
    ocr_missing = False
    if is_low_signal_document(document_input):
        if config.ocr_enabled:
            ocr_bytes = try_ocr_pdf(path, config.ocr_command)
            if ocr_bytes is not None:
                ocr_page_texts = extract_pdf_page_texts(ocr_bytes)
                ocr_input = DocumentInput(
                    source_name=path.name,
                    pdf_bytes=ocr_bytes,
                    fallback_date=datetime.fromtimestamp(stat.st_mtime, tz=timezone.utc),
                    page_texts=ocr_page_texts,
                    extracted_text="\n".join(ocr_page_texts).strip(),
                )
                if not is_low_signal_document(ocr_input):
                    pdf_bytes = ocr_bytes
                    document_input = ocr_input
                    uses_ocr = True
                else:
                    ocr_missing = True
            else:
                ocr_missing = True
        else:
            ocr_missing = True

    document_input = DocumentInput(
        source_name=path.name,
        pdf_bytes=pdf_bytes,
        fallback_date=datetime.fromtimestamp(stat.st_mtime, tz=timezone.utc),
        page_texts=document_input.page_texts,
        extracted_text=document_input.extracted_text,
    )
    return PendingScan(
        source_path=path,
        document_input=document_input,
        stat_size=stat.st_size,
        stat_mtime_ns=stat.st_mtime_ns,
        original_bytes=original_bytes,
        uses_ocr=uses_ocr,
        ocr_missing=ocr_missing,
    )


def record_processed_file(
    manifest: dict[str, Any],
    pending: PendingScan,
    output_path: Path,
    title: str,
    document_date: str,
    original_saved_path: Optional[Path] = None,
) -> None:
    source_key = str(pending.source_path.resolve())
    record = {
        "source_path": source_key,
        "size": pending.stat_size,
        "mtime_ns": pending.stat_mtime_ns,
        "saved_path": str(output_path),
        "saved_filename": output_path.name,
        "title": title,
        "document_date": document_date,
        "uses_ocr": pending.uses_ocr,
        "ocr_missing": pending.ocr_missing,
    }
    if original_saved_path is not None:
        record["original_saved_path"] = str(original_saved_path)
    processed = dict(manifest.get("processed_files", {}))
    processed[source_key] = record
    manifest["processed_files"] = processed

    files = [entry for entry in manifest.get("files", []) if entry.get("source_path") != source_key]
    files.append(record)
    manifest["files"] = sorted(files, key=lambda item: str(item.get("saved_filename", "")))
    manifest["updated_at"] = datetime.now(timezone.utc).isoformat()


def persist_scan(config: RuntimeConfig, pending: PendingScan, decision, manifest: dict[str, Any]) -> Path:
    base_relative_path = build_relative_output_path(
        decision,
        include_category=False,
        filename_suffix="",
        folder_style="nested_year_monthword",
    )
    suffix_parts = ["-Codex"]
    if decision.needs_review:
        suffix_parts.append("-ToReview")
    if pending.ocr_missing:
        suffix_parts.append("-OCRMissing")
    suffix = "".join(suffix_parts)
    relative_path = build_relative_output_path(
        decision,
        include_category=False,
        filename_suffix=suffix,
        folder_style="nested_year_monthword",
    )
    output_path = unique_output_path(config.output_dir / relative_path.parent, relative_path.name)

    if config.dry_run:
        print(f"[dry-run] {pending.source_path} -> {output_path}")
        return output_path

    output_path.parent.mkdir(parents=True, exist_ok=True)
    original_output_path = None
    if pending.uses_ocr:
        output_path.write_bytes(pending.document_input.pdf_bytes)
        if config.preserve_original_on_ocr:
            original_output_path = unique_output_path(
                output_path.parent,
                f"{Path(base_relative_path.name).stem} [Original]{output_path.suffix}",
            )
            original_output_path.write_bytes(pending.original_bytes)
        if config.mode == "move":
            pending.source_path.unlink()
    else:
        if config.mode == "copy":
            shutil.copy2(pending.source_path, output_path)
        else:
            shutil.move(str(pending.source_path), str(output_path))

    record_processed_file(
        manifest,
        pending,
        output_path,
        title=decision.title,
        document_date=decision.document_date.isoformat(),
        original_saved_path=original_output_path,
    )
    save_manifest(config.state_file, manifest)
    print(f"{pending.source_path} -> {output_path}")
    return output_path


def main() -> int:
    args = parse_args()
    config = build_runtime_config(args)

    if config.mode not in {"move", "copy"}:
        print("output.mode must be 'move' or 'copy'", file=sys.stderr)
        return 2
    if not config.input_root.exists():
        print(f"Missing input root: {config.input_root}", file=sys.stderr)
        return 2

    debug(f"Input root: {config.input_root}")
    debug(f"Output dir: {config.output_dir}")
    debug(f"State file: {config.state_file}")
    debug(f"Mode: {config.mode}")
    debug(
        f"OCR enabled={config.ocr_enabled} command={config.ocr_command} "
        f"preserve_original_on_ocr={config.preserve_original_on_ocr}"
    )
    if config.lm_config:
        debug(
            f"LM Studio enabled model={config.lm_config.model} base_url={config.lm_config.base_url} "
            f"batch_size={config.lm_config.batch_size} max_input_tokens={config.lm_config.max_input_tokens} "
            f"debug={config.lm_config.debug}"
        )

    manifest = load_manifest(config.state_file)
    matches = iter_matching_files(config, manifest)
    print(f"Matched {len(matches)} scan file(s).")
    if not matches:
        return 0

    processed_count = 0
    pending_batch: list[PendingScan] = []
    flush_size = config.lm_config.batch_size if config.lm_config else 1
    total_matches = len(matches)

    def flush_batch(batch: list[PendingScan], completed_before: int) -> int:
        if not batch:
            return 0
        print(f"About to process {len(batch)}/{total_matches} ({completed_before})")
        decisions = analyze_documents_batch(
            [pending.document_input for pending in batch],
            lm_config=config.lm_config,
            debug_logger=debug,
        )
        for pending, decision in zip(batch, decisions):
            persist_scan(config, pending, decision, manifest)
        return len(batch)

    for path in matches:
        pending_batch.append(build_pending_scan(path, config))
        if len(pending_batch) >= flush_size:
            processed_count += flush_batch(pending_batch, processed_count)
            pending_batch = []

    processed_count += flush_batch(pending_batch, processed_count)
    print(f"Processed {processed_count} file(s).")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
