#!/usr/bin/env python3
"""Verify every compact occurrence against pinned CSVs and optionally the v2 artifact."""
from __future__ import annotations

import argparse
from contextlib import closing
import json
from pathlib import Path
import sqlite3

from build_index import (
    BuildError, OUTPUT_MANIFEST, canonical, load_sources_config, read_sources,
    sha256_file, validate_outputs, verify_source_rows,
)


def compare_v2_rows(connection: sqlite3.Connection, expected: list[dict]) -> dict:
    """Compare full canonical raw objects and occurrence keys, never ISBN/title."""
    cursor = connection.execute(
        "SELECT source_key,source_row,raw_record_json FROM books ORDER BY id",
    )
    checked = 0
    for book in expected:
        row = cursor.fetchone()
        if row is None:
            raise BuildError("v2 reference is missing a source occurrence")
        if row[:2] != (book["source_key"], book["source_row"]):
            raise BuildError(f"v2 provenance mismatch at source row {book['source_row']}")
        original = json.loads(row[2])
        if canonical(original) != canonical(book["_raw_record"]):
            fields = sorted(key for key in original.keys() | book["_raw_record"].keys()
                            if key not in original or key not in book["_raw_record"]
                            or original[key] != book["_raw_record"][key])
            raise BuildError(f"v2 full raw mismatch at source row {book['source_row']}; fields={fields}")
        checked += 1
    if cursor.fetchone() is not None:
        raise BuildError("v2 reference has an extra source occurrence")
    return {"rows_checked": checked, "mismatch_count": 0}


def verify_index(output_dir: Path, resources: list[dict], reference_v2: Path | None = None) -> dict:
    validate_outputs(output_dir)
    manifest = json.loads((output_dir / OUTPUT_MANIFEST).read_text(encoding="utf-8"))
    sources, expected = read_sources(resources)
    if sources != manifest["sources"]:
        raise BuildError("Pinned source provenance/retrieval metadata differs from manifest")
    path = output_dir / manifest["sqlite_asset"]
    with closing(sqlite3.connect(path.resolve().as_uri() + "?mode=ro&immutable=1", uri=True)) as c:
        report = {"csv_to_v3": verify_source_rows(c, expected)}
        report["source_counts"] = c.execute("SELECT month,record_count FROM sources ORDER BY month,identifier").fetchall()
        report["layout_shapes"] = [len(json.loads(row[0])) for row in c.execute("SELECT keys_json FROM raw_layouts ORDER BY id")]
        report["page_size"] = c.execute("PRAGMA page_size").fetchone()[0]
        report["page_count"] = c.execute("PRAGMA page_count").fetchone()[0]
        report["freelist_count"] = c.execute("PRAGMA freelist_count").fetchone()[0]
        report["integrity"] = c.execute("PRAGMA integrity_check").fetchall()
        try:
            report["dbstat"] = c.execute(
                "SELECT name,count(*),sum(pgsize),sum(payload),sum(unused) FROM dbstat GROUP BY name ORDER BY sum(pgsize) DESC",
            ).fetchall()
        except sqlite3.OperationalError as error:
            report["dbstat_unavailable"] = str(error)
    if reference_v2 is not None:
        old = json.loads((reference_v2 / OUTPUT_MANIFEST).read_text(encoding="utf-8"))
        if old["sqlite_schema_version"] != 2 or old["sources"] != manifest["sources"]:
            raise BuildError("v2 reference schema/source provenance differs")
        old_path = reference_v2 / old["sqlite_asset"]
        if sha256_file(old_path) != old["sqlite_sha256"]:
            raise BuildError("v2 reference hash mismatch")
        with closing(sqlite3.connect(old_path.resolve().as_uri() + "?mode=ro&immutable=1", uri=True)) as c:
            report["v2_to_csv"] = compare_v2_rows(c, expected)
        # Both complete comparisons above use the same full original raw objects.
        report["v2_to_v3"] = {"rows_checked": len(expected), "mismatch_count": 0}
    report.update({key: manifest[key] for key in ("record_count", "coverage_start", "coverage_end",
                   "uncompressed_size", "compressed_size", "sqlite_sha256", "gzip_sha256", "content_version")})
    return report


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--sources", type=Path, required=True)
    parser.add_argument("--reference-v2", type=Path)
    args = parser.parse_args()
    try:
        report = verify_index(args.output_dir, load_sources_config(args.sources), args.reference_v2)
    except (BuildError, OSError, ValueError, sqlite3.Error, KeyError, TypeError) as error:
        print(f"error: {error}")
        return 1
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
