#!/usr/bin/env python3
"""Replace one top-level AppDaemon app block in apps.yaml."""

from __future__ import annotations

import argparse
from pathlib import Path


def block_bounds(lines: list[str], section: str) -> tuple[int, int] | None:
    start = None
    section_prefix = f"{section}:"

    for index, line in enumerate(lines):
        if start is None:
            if line.startswith(section_prefix):
                start = index
            continue

        if line and not line.startswith((" ", "\t", "#", "\n", "\r")):
            return start, index

    if start is None:
        return None
    return start, len(lines)


def read_block(path: Path, section: str) -> list[str]:
    lines = path.read_text(encoding="utf-8").splitlines(keepends=True)
    bounds = block_bounds(lines, section)
    if bounds is None:
        raise SystemExit(f"{path} bevat geen top-level sectie {section}:")
    start, end = bounds
    block = lines[start:end]
    if block and not block[-1].endswith(("\n", "\r")):
        block[-1] += "\n"
    return block


def merge(remote_path: Path, source_path: Path, output_path: Path, section: str) -> None:
    source_block = read_block(source_path, section)
    remote_text = remote_path.read_text(encoding="utf-8") if remote_path.exists() else ""
    remote_lines = remote_text.splitlines(keepends=True)
    bounds = block_bounds(remote_lines, section)

    if bounds is None:
        merged_lines = remote_lines[:]
        if merged_lines and merged_lines[-1].strip():
            merged_lines.append("\n")
        merged_lines.extend(source_block)
    else:
        start, end = bounds
        replacement_block = source_block[:]
        if end < len(remote_lines) and replacement_block[-1].strip():
            replacement_block.append("\n")
        merged_lines = remote_lines[:start] + replacement_block + remote_lines[end:]

    output_path.write_text("".join(merged_lines), encoding="utf-8")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--section", required=True)
    parser.add_argument("--source", type=Path, required=True)
    parser.add_argument("--remote", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()

    merge(args.remote, args.source, args.output, args.section)


if __name__ == "__main__":
    main()
