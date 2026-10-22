import csv
from contextlib import closing
import gzip
import json
import os
import shutil
import subprocess
import sys
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from verify_index import compare_v2_rows, verify_index

from build_index import (
    APPLICATION_ID, FORMAT_VERSION, NORMALIZATION_VERSION, OUTPUT_MANIFEST, SQLITE_SCHEMA_VERSION,
    SOURCE_PAGE, BuildError, build_index, digest_bytes, discover_sources, isbn10_to_isbn13,
    is_valid_isbn10, is_valid_isbn13, load_sources_config, normalize_text, normalized_isbns,
    parse_isbns, sha256_file, validate_outputs, reconstruct_raw_record, RAW_LAYOUT_VERSION,
    canonical, RAW_FIELDS, read_sources, verify_source_rows, content_version,
)

HEADERS = ["申請書名", "作者", "出版機構", "預訂出版日", "ISBN", "頁數", "作品語文",
           "作品語文(其他)", "出版形式", "版次", "裝訂方式", "其他裝訂方式", "額外欄位"]


class BuildIndexTest(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.counter = 0

    def row(self, **changes):
        return {"申請書名": " 測試「書名」 ", "作者": "王小明著; 李小華譯", "出版機構": "出版社",
                "預訂出版日": "2026-03-00", "ISBN": "9780306406157", "頁數": "１２０",
                "作品語文": "中文", "作品語文(其他)": "", "出版形式": "單行本", "版次": "初版",
                "裝訂方式": "平裝", "其他裝訂方式": "", "額外欄位": "不可丟失", **changes}

    def source(self, month="2026-03", rows=None, **changes):
        headers = changes.pop("headers", HEADERS)
        self.counter += 1
        path = self.root / f"source-{self.counter}.csv"
        with path.open("w", encoding="utf-8-sig", newline="") as stream:
            writer = csv.DictWriter(stream, fieldnames=headers)
            writer.writeheader()
            writer.writerows(rows if rows is not None else [self.row()])
        return {"month": month, "url": f"https://isbn.ncl.edu.tw/test-{self.counter}.csv",
                "path": path, "sha256": sha256_file(path), "retrieved_at": "2026-10-22T00:00:00Z", **changes}

    def build(self, resources, directory="out", **kwargs):
        output = self.root / directory
        manifest = build_index(output, resources, built_at=kwargs.pop("built_at", "2026-10-22T00:00:00Z"), **kwargs)
        return output, manifest

    def query(self, output, manifest, sql):
        with closing(sqlite3.connect((output / manifest["sqlite_asset"]).resolve().as_uri() + "?mode=ro", uri=True)) as connection:
            return connection.execute(sql).fetchall()

    def reconstructed(self, output, manifest):
        with closing(sqlite3.connect((output / manifest["sqlite_asset"]).resolve().as_uri() + "?mode=ro", uri=True)) as c:
            c.row_factory = sqlite3.Row
            layouts = {row["id"]: dict(row) for row in c.execute("SELECT * FROM raw_layouts")}
            return [reconstruct_raw_record(dict(row), layouts[row["raw_layout_id"]])
                    for row in c.execute("SELECT * FROM books ORDER BY id")]

    def test_schema_v3_integer_keys_removed_legacy_columns_and_indexes(self):
        out, m = self.build([self.source(rows=[self.row(), self.row()])])
        self.assertEqual(3, m["sqlite_schema_version"])
        self.assertEqual(1, m["normalization_version"])
        self.assertEqual([("integer", "integer", "integer", "integer")], self.query(out, m,
            "SELECT typeof(id),typeof(source_id),typeof(source_row),typeof(raw_layout_id) FROM books LIMIT 1"))
        self.assertEqual([("integer",)], self.query(out, m, "SELECT typeof(id) FROM sources"))
        columns = {row[1] for row in self.query(out, m, "PRAGMA table_info(books)")}
        self.assertFalse({"source_identifier", "source_key", "raw_record_json"} & columns)
        indexes = self.query(out, m, "PRAGMA index_list(books)")
        self.assertEqual(4, len(indexes))
        self.assertEqual({"books_normalized_isbn_idx", "books_normalized_title_idx", "books_normalized_author_idx"},
                         {row[1] for row in indexes if not row[2]})
        unique = next(row[1] for row in indexes if row[2])
        self.assertEqual(["source_id", "source_row"], [row[2] for row in self.query(out, m, 'PRAGMA index_info("' + unique + '")')])
        self.assertEqual([], self.query(out, m, "SELECT name FROM sqlite_master WHERE upper(sql) LIKE '%CREATE VIRTUAL TABLE%'"))

    def test_unique_source_row_is_enforced(self):
        out, m = self.build([self.source()])
        path = out / m["sqlite_asset"]
        with closing(sqlite3.connect(path)) as c, c:
            columns = [row[1] for row in c.execute("PRAGMA table_info(books)") if row[1] != "id"]
            with self.assertRaises(sqlite3.IntegrityError):
                c.execute("INSERT INTO books (" + ",".join(columns) + ") SELECT " + ",".join(columns) + " FROM books")

    def test_all_three_verified_header_shapes_round_trip(self):
        fixture = json.loads(Path(__file__).with_name("raw_layout_fixtures.json").read_text(encoding="utf-8"))
        self.assertEqual(RAW_LAYOUT_VERSION, fixture["layout_version"])
        self.assertEqual([27, 27, 28], sorted(len(layout["keys"]) for layout in fixture["layouts"]))
        resources, originals = [], []
        for index, layout in enumerate(fixture["layouts"]):
            raw = {key: self.row().get(key, "") for key in layout["keys"]}
            # Exercise distinct legacy names and blank values without role interpretation.
            for key in ("常用分類", "建議上架分類", "是否為翻譯書", "是否為引進版權著作"):
                if key in raw:
                    raw[key] = " 原始 " + key + " "
            originals.append(raw)
            resources.append(self.source(f"2026-0{index + 1}", [raw], headers=layout["keys"]))
        out, m = self.build(resources)
        reconstructed = self.reconstructed(out, m)
        self.assertEqual(sorted(map(canonical, originals)), sorted(map(canonical, reconstructed)))
        self.assertEqual([(3,)], self.query(out, m, "SELECT count(*) FROM raw_layouts"))
        self.assertEqual({"rows_checked": 3, "mismatch_count": 0, "sources_checked": 3}, m["source_round_trip"])
        reverse_out, reverse_manifest = self.build(list(reversed(resources)), "reversed-layouts")
        self.assertEqual(m, reverse_manifest)
        self.assertEqual((out / m["sqlite_asset"]).read_bytes(), (reverse_out / m["sqlite_asset"]).read_bytes())
        self.assertTrue(any("" in raw and raw[""] == "" for raw in reconstructed))

    def test_shared_layout_and_positional_unknown_empty_and_missing_fields(self):
        raw = self.row(**{"未知新欄位": "  Ａ\t原始值  ", "": ""})
        missing = {key: value for key, value in raw.items() if key not in ("edition", "版次", "未知新欄位", "")}
        resources = [self.source(rows=[raw, raw], headers=list(raw)),
                     self.source("2026-04", [missing], headers=list(missing))]
        out, m = self.build(resources)
        actual = self.reconstructed(out, m)
        self.assertEqual(sorted(map(canonical, [raw, raw, missing])), sorted(map(canonical, actual)))
        self.assertEqual([(2,)], self.query(out, m, "SELECT count(*) FROM raw_layouts"))
        self.assertEqual([1, 2], sorted(row[0] for row in self.query(out, m, "SELECT count(*) FROM books GROUP BY raw_layout_id")))
        for (encoded,) in self.query(out, m, "SELECT raw_extras_values_json FROM books"):
            self.assertIsInstance(json.loads(encoded), list)
            self.assertNotIn("未知新欄位", encoded)
        raw_missing = next(record for record in actual if "未知新欄位" not in record)
        self.assertNotIn("版次", raw_missing)
        self.assertTrue(any(record.get("未知新欄位") == "  Ａ\t原始值  " for record in actual))

    def test_full_source_round_trip_detects_unmapped_value_corruption(self):
        resource = self.source()
        out, m = self.build([resource])
        _, expected = read_sources([resource])
        with closing(sqlite3.connect(out / m["sqlite_asset"])) as c, c:
            c.execute("UPDATE books SET raw_extras_values_json='[\"changed\"]'")
            with self.assertRaisesRegex(BuildError, "Raw round-trip mismatch"):
                verify_source_rows(c, expected)

    def test_source_round_trip_checks_provenance_and_extra_rows(self):
        resource = self.source()
        out, m = self.build([resource])
        _, expected = read_sources([resource])
        with closing(sqlite3.connect(out / m["sqlite_asset"])) as c, c:
            c.execute("UPDATE books SET source_row=100")
            with self.assertRaisesRegex(BuildError, "Provenance mismatch"):
                verify_source_rows(c, expected)

    def test_rejects_invalid_layout_and_extras(self):
        self.corrupt_database("UPDATE raw_layouts SET layout_version=99")
        self.corrupt_database("UPDATE books SET raw_extras_values_json='[]'")

    def test_schema_change_changes_content_identity_but_not_normalization(self):
        out, m = self.build([self.source()])
        with patch("build_index.SQLITE_SCHEMA_VERSION", 2), patch("build_index.BUILDER_VERSION", "2"):
            previous = content_version(m["sources"])
        self.assertNotEqual(previous, m["content_version"])
        self.assertEqual(1, m["normalization_version"])

    def test_independent_verifier_checks_source_rows_and_source_metadata(self):
        resource = self.source(rows=[self.row(), self.row()])
        out, m = self.build([resource])
        report = verify_index(out, [resource])
        self.assertEqual({"rows_checked": 2, "mismatch_count": 0, "sources_checked": 1}, report["csv_to_v3"])
        self.assertEqual(2, report["record_count"])
        with self.assertRaisesRegex(BuildError, "provenance/retrieval"):
            verify_index(out, [{**resource, "retrieved_at": "different observation"}])

    def test_v2_reference_comparison_requires_complete_payload_and_provenance(self):
        resource = self.source()
        _, expected = read_sources([resource])
        with closing(sqlite3.connect(":memory:")) as c, c:
            c.execute("CREATE TABLE books (id INTEGER PRIMARY KEY,source_key TEXT,source_row INTEGER,raw_record_json TEXT)")
            book = expected[0]
            c.execute("INSERT INTO books VALUES (1,?,?,?)", (book["source_key"], book["source_row"], canonical(book["_raw_record"])))
            self.assertEqual({"rows_checked": 1, "mismatch_count": 0}, compare_v2_rows(c, expected))
            altered = {**book["_raw_record"], "額外欄位": "altered extra"}
            c.execute("UPDATE books SET raw_record_json=?", (canonical(altered),))
            with self.assertRaisesRegex(BuildError, "full raw mismatch"):
                compare_v2_rows(c, expected)
            c.execute("UPDATE books SET source_row=99")
            with self.assertRaisesRegex(BuildError, "provenance mismatch"):
                compare_v2_rows(c, expected)

    def test_v2_reference_comparison_detects_missing_and_extra_occurrences(self):
        _, expected = read_sources([self.source()])
        with closing(sqlite3.connect(":memory:")) as c, c:
            c.execute("CREATE TABLE books (id INTEGER PRIMARY KEY,source_key TEXT,source_row INTEGER,raw_record_json TEXT)")
            with self.assertRaisesRegex(BuildError, "missing"):
                compare_v2_rows(c, expected)
            book = expected[0]
            for index in (1, 2):
                c.execute("INSERT INTO books VALUES (?,?,?,?)", (index, book["source_key"], book["source_row"], canonical(book["_raw_record"])))
            with self.assertRaisesRegex(BuildError, "extra"):
                compare_v2_rows(c, expected)

    def test_multiple_months_coverage_and_provenance(self):
        a, b = self.source(), self.source("2026-05")
        out, m = self.build([b, a])
        self.assertEqual(2, m["record_count"])
        self.assertEqual(["2026-03", "2026-05"], m["coverage_months"])
        self.assertEqual(("2026-03", "2026-05"), (m["coverage_start"], m["coverage_end"]))
        self.assertIn("not_complete", m["coverage_scope"])
        self.assertEqual(2, len(self.query(out, m, "SELECT * FROM sources")))
        self.assertEqual([(2,)], self.query(out, m, "SELECT count(*) FROM books b JOIN sources s ON b.source_id=s.id"))
        self.assertTrue(all(s["sha256"] and s["retrieved_at"] for s in m["sources"]))

    def test_equivalent_input_order_produces_identical_outputs(self):
        a, b = self.source(), self.source("2026-04")
        o1, m1 = self.build([a, b], "first")
        o2, m2 = self.build([b, a], "second")
        self.assertEqual(m1, m2)
        for name in (m1["sqlite_asset"], m1["data_asset"], OUTPUT_MANIFEST):
            self.assertEqual((o1 / name).read_bytes(), (o2 / name).read_bytes())

    def test_same_isbn_title_different_edition_preserved(self):
        out, m = self.build([self.source(rows=[self.row(), self.row(版次="增訂二版")])])
        self.assertEqual(2, m["record_count"])
        self.assertEqual({"初版", "增訂二版"}, {r[0] for r in self.query(out, m, "SELECT edition_raw FROM books")})

    def test_same_isbn_title_different_binding_preserved(self):
        out, m = self.build([self.source(rows=[self.row(), self.row(裝訂方式="精裝")])])
        self.assertEqual(2, m["record_count"])
        self.assertEqual({"平裝", "精裝"}, {r[0] for r in self.query(out, m, "SELECT binding_raw FROM books")})

    def test_corrected_metadata_across_months_preserved(self):
        a = self.source()
        b = self.source("2026-04", [self.row(作者="王小明編著", 出版機構="更正出版社")])
        out, m = self.build([a, b])
        self.assertEqual(2, m["record_count"])
        self.assertEqual({"出版社", "更正出版社"}, {r[0] for r in self.query(out, m, "SELECT publisher FROM books")})

    def test_exact_duplicate_occurrences_keep_all_provenance(self):
        a = self.source(rows=[self.row(), self.row()])
        b = self.source("2026-04")
        out, m = self.build([a, b])
        self.assertEqual(3, m["record_count"])
        self.assertEqual([(3, 3)], self.query(out, m, "SELECT count(*),count(DISTINCT id) FROM books"))
        self.assertEqual([1, 2], sorted(r[0] for r in self.query(out, m, "SELECT record_count FROM sources")))

    def test_raw_columns_unknown_fields_and_partial_dates_unchanged(self):
        rows = [self.row(預訂出版日=d) for d in ("2026-03-00", "2026-00-00", "2026", "不詳")]
        out, m = self.build([self.source(rows=rows)])
        records = self.query(out, m, "SELECT title,author_raw,publication_date_raw FROM books ORDER BY source_row")
        self.assertEqual([r["預訂出版日"] for r in rows], [r[2] for r in records])
        self.assertEqual(rows[0]["申請書名"], records[0][0])
        self.assertEqual(rows[0]["作者"], records[0][1])
        self.assertEqual(rows[0], self.reconstructed(out, m)[0])

    def test_invalid_isbn_remains_raw_without_valid_key(self):
        out, m = self.build([self.source(rows=[self.row(ISBN="9780306406158")])])
        self.assertEqual([("9780306406158", None, None, None, "[]")], self.query(
            out, m, "SELECT isbn_raw,isbn10,isbn13,normalized_isbn,valid_isbns_json FROM books"))

    def test_normalization_portable_fixtures(self):
        fixtures = json.loads(Path(__file__).with_name("normalization_fixtures.json").read_text(encoding="utf-8"))
        self.assertEqual(NORMALIZATION_VERSION, fixtures["normalization_version"])
        for case in fixtures["text"]:
            with self.subTest(case=case):
                self.assertEqual(case["expected"], normalize_text(case["input"]))
        for case in fixtures["isbn"]:
            with self.subTest(case=case):
                self.assertEqual((case["isbn10"], case["isbn13"], case["comparison"]), parse_isbns(case["input"]))

    def test_isbn10_x_and_multiple_identifiers_remain_consistent(self):
        self.assertTrue(is_valid_isbn10("080442957X"))
        self.assertTrue(is_valid_isbn13("9780804429573"))
        self.assertEqual("9780804429573", isbn10_to_isbn13("080442957X"))
        values = normalized_isbns("080442957X;9780306406157")
        self.assertEqual(2, len(values))
        isbn10, _, comparison = parse_isbns("080442957X;9780306406157")
        self.assertEqual(isbn10_to_isbn13(isbn10), comparison)

    def test_built_and_retrieved_timestamps_are_not_content_identity(self):
        a = self.source()
        o1, m1 = self.build([a], "one")
        o2, m2 = self.build([{**a, "retrieved_at": "2027-01-01T00:00:00Z"}], "two", built_at="2027-01-01T00:00:00Z")
        self.assertNotEqual(m1["built_at"], m2["built_at"])
        self.assertEqual(m1["content_version"], m2["content_version"])
        self.assertEqual((o1 / m1["sqlite_asset"]).read_bytes(), (o2 / m2["sqlite_asset"]).read_bytes())
        self.assertEqual(m1["gzip_sha256"], m2["gzip_sha256"])

    def test_content_changes_create_new_version_and_keep_old_asset(self):
        a = self.source()
        out, m1 = self.build([a])
        _, m2 = self.build([self.source(rows=[self.row(版次="二版")])])
        self.assertNotEqual(m1["content_version"], m2["content_version"])
        self.assertTrue((out / m1["data_asset"]).is_file())
        self.assertTrue((out / m2["data_asset"]).is_file())

    def test_hashes_sizes_header_schema_integrity_and_indexes(self):
        out, m = self.build([self.source()])
        validate_outputs(out)
        sqlite_path, gzip_path = out / m["sqlite_asset"], out / m["data_asset"]
        self.assertEqual(FORMAT_VERSION, m["format_version"])
        self.assertEqual(SQLITE_SCHEMA_VERSION, m["sqlite_schema_version"])
        self.assertEqual(sqlite_path.stat().st_size, m["uncompressed_size"])
        self.assertEqual(gzip_path.stat().st_size, m["compressed_size"])
        self.assertEqual(sha256_file(sqlite_path), m["sqlite_sha256"])
        self.assertEqual(sha256_file(gzip_path), m["gzip_sha256"])
        self.assertEqual(sqlite_path.read_bytes(), gzip.decompress(gzip_path.read_bytes()))
        self.assertEqual(b"SQLite format 3\x00", sqlite_path.read_bytes()[:16])
        self.assertEqual([(APPLICATION_ID,)], self.query(out, m, "PRAGMA application_id"))
        self.assertEqual([(SQLITE_SCHEMA_VERSION,)], self.query(out, m, "PRAGMA user_version"))
        self.assertEqual([("ok",)], self.query(out, m, "PRAGMA integrity_check"))

    def corrupt_database(self, sql):
        out, m = self.build([self.source()])
        path = out / m["sqlite_asset"]
        with closing(sqlite3.connect(path)) as c, c:
            c.execute(sql)
        # Rehash to exercise structural validation rather than only hash validation.
        m["sqlite_sha256"] = sha256_file(path)
        m["uncompressed_size"] = path.stat().st_size
        (out / m["data_asset"]).write_bytes(gzip.compress(path.read_bytes(), mtime=0))
        m["gzip_sha256"] = sha256_file(out / m["data_asset"])
        m["compressed_size"] = (out / m["data_asset"]).stat().st_size
        (out / OUTPUT_MANIFEST).write_text(json.dumps(m), encoding="utf-8")
        with self.assertRaises(BuildError):
            validate_outputs(out)

    def test_rejects_wrong_schema(self):
        self.corrupt_database("PRAGMA user_version=99")

    def test_rejects_wrong_application_identity(self):
        self.corrupt_database("PRAGMA application_id=0")

    def test_rejects_missing_required_column(self):
        self.corrupt_database("ALTER TABLE books RENAME COLUMN edition_raw TO lost_edition")

    def test_rejects_bad_header(self):
        out, m = self.build([self.source()])
        path = out / m["sqlite_asset"]
        body = bytearray(path.read_bytes()); body[0] = 0
        path.write_bytes(body)
        m["sqlite_sha256"] = sha256_file(path)
        (out / OUTPUT_MANIFEST).write_text(json.dumps(m), encoding="utf-8")
        with self.assertRaisesRegex(BuildError, "header"):
            validate_outputs(out)

    def test_rejects_gzip_tampering(self):
        out, m = self.build([self.source()])
        (out / m["data_asset"]).write_bytes(b"bad")
        with self.assertRaises(BuildError):
            validate_outputs(out)

    def test_rejects_manifest_coverage_tampering(self):
        out, m = self.build([self.source()])
        m["coverage_start"] = "1900-01"
        (out / OUTPUT_MANIFEST).write_text(json.dumps(m), encoding="utf-8")
        with self.assertRaises(BuildError):
            validate_outputs(out)

    def test_validation_failure_keeps_previous_manifest_and_asset(self):
        a = self.source()
        out, m = self.build([a])
        previous = (out / OUTPUT_MANIFEST).read_bytes()
        with patch("build_index.validate_outputs", side_effect=BuildError("broken")):
            with self.assertRaises(BuildError):
                self.build([self.source("2026-04")])
        self.assertEqual(previous, (out / OUTPUT_MANIFEST).read_bytes())
        self.assertTrue((out / m["data_asset"]).is_file())

    def test_immutable_filename_conflict_fails_before_manifest(self):
        a = self.source()
        out, m = self.build([a])
        previous = (out / OUTPUT_MANIFEST).read_bytes()
        (out / m["data_asset"]).write_bytes(b"conflict")
        with self.assertRaisesRegex(BuildError, "Immutable"):
            self.build([a])
        self.assertEqual(previous, (out / OUTPUT_MANIFEST).read_bytes())

    def test_discovery_enumerates_only_explicit_month_links(self):
        html = b'<div>2025\xe5\xb9\xb4 <a href="/a.csv">12\xe6\x9c\x88</a></div><div>2026\xe5\xb9\xb4 <a href="/b.csv">2\xe6\x9c\x88</a> 3\xe6\x9c\x88</div>'
        with patch("build_index.request_bytes", return_value=(html, None, "utf-8")):
            sources = discover_sources()
        self.assertEqual(["2025-12", "2026-02"], [s["month"] for s in sources])
        self.assertEqual(["https://isbn.ncl.edu.tw/a.csv", "https://isbn.ncl.edu.tw/b.csv"], [s["url"] for s in sources])

    def test_discovery_failure_never_falls_back_to_guessed_urls(self):
        with patch("build_index.request_bytes", return_value=(b"no links", None, "utf-8")):
            with self.assertRaises(BuildError):
                discover_sources()

    def test_rejects_unofficial_url_bad_month_hash_and_duplicate_source(self):
        a = self.source()
        for resource in ({**a, "url": "https://example.com/a.csv"}, {**a, "month": "unknown"},
                         {**a, "sha256": "0" * 64}, {k: v for k, v in a.items() if k != "sha256"}):
            with self.subTest(resource=resource), self.assertRaises(BuildError):
                self.build([resource])
        with self.assertRaises(BuildError):
            self.build([a, a])

    def test_downloaded_source_hash_and_retrieval_are_recorded(self):
        a = self.source()
        body = a["path"].read_bytes()
        a.pop("path"); a.pop("retrieved_at")
        with patch("build_index.request_bytes", return_value=(body, "yesterday", "utf-8")):
            _, m = self.build([a])
        self.assertEqual(digest_bytes(body), m["sources"][0]["sha256"])
        self.assertTrue(m["sources"][0]["retrieved_at"].endswith("Z"))

    def test_rejects_malformed_csv(self):
        a = self.source()
        for body in (b"<html>error</html>", b"", b"bad,header\n", a["path"].read_bytes() + b"missing,column\n"):
            a["path"].write_bytes(body); a["sha256"] = digest_bytes(body)
            with self.subTest(body=body[:40]), self.assertRaises(BuildError):
                self.build([a])

    def test_source_config_paths_are_relative_to_config_not_cwd(self):
        a = self.source()
        config = self.root / "sources.json"
        config.write_text(json.dumps({"sources": [{**a, "path": a["path"].name}]}), encoding="utf-8")
        self.assertEqual(a["path"], load_sources_config(config)[0]["path"])

    def workflow_steps(self):
        path = Path(__file__).resolve().parents[2] / ".github/workflows/build-taiwan-new-books-index.yml"
        steps = {}
        for block in path.read_text().split("      - name: ")[1:]:
            if "        run: |\n" in block:
                name = block.splitlines()[0]
                steps[name] = "\n".join(line[10:] if line.startswith("          ") else line
                                         for line in block.split("        run: |\n", 1)[1].splitlines())
        return steps

    def run_workflow(self, out, names, fail_upload=False):
        # Run the actual shell steps against an isolated fake gh; no account/network writes.
        binary = self.root / "bin"
        binary.mkdir(exist_ok=True)
        remote = self.root / "remote"
        remote.mkdir(exist_ok=True)
        (self.root / "build").mkdir(exist_ok=True)
        module_dir = self.root / "scripts/taiwan_new_books"
        module_dir.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(Path(__file__).with_name("build_index.py"), module_dir / "build_index.py")
        gh = binary / "gh"
        gh.write_text("#!" + sys.executable + "\n" + r"""
import fnmatch, json, os, shutil, sys
from pathlib import Path
root = Path(os.environ['FAKE_REMOTE'])
a = sys.argv[1:]
with (root.parent / 'gh-log').open('a') as log: log.write(json.dumps(a) + '\n')
assert a[0] == 'release'
op = a[1]
if op == 'view':
    if not (root / '.release').exists(): sys.exit(1)
    print(json.dumps({'assets': [{'name': p.name} for p in root.iterdir() if p.is_file() and not p.name.startswith('.')]}))
elif op == 'create':
    (root / '.release').touch()
elif op == 'upload':
    source = Path(a[-1])
    if os.environ.get('FAIL_UPLOAD') == '1' and source.name.endswith('.gz'): sys.exit(42)
    destination = root / source.name
    if destination.exists() and '--clobber' not in a: sys.exit(1)
    if source.name == 'taiwan-new-books-manifest.json':
        m = json.loads(source.read_text())
        if not (root / m['data_asset']).is_file(): sys.exit(43)
    shutil.copyfile(source, destination)
elif op == 'download':
    pattern = a[a.index('--pattern') + 1]
    directory = Path(a[a.index('--dir') + 1]); directory.mkdir(parents=True, exist_ok=True)
    matches = [p for p in root.iterdir() if fnmatch.fnmatch(p.name, pattern)]
    if not matches: sys.exit(1)
    for source in matches:
        destination = directory / source.name
        if destination.exists() and '--clobber' not in a: sys.exit(1)
        shutil.copyfile(source, destination)
else: sys.exit(2)
""", encoding="utf-8")
        gh.chmod(0o755)
        env = {**os.environ, "PATH": str(binary) + os.pathsep + os.environ['PATH'],
               "OUTPUT_DIR": str(out), "RELEASE_TAG": "test-index", "FAKE_REMOTE": str(remote),
               "GITHUB_OUTPUT": str(self.root / "github-output"), "FAIL_UPLOAD": "1" if fail_upload else "0"}
        result = None
        for name in names:
            result = subprocess.run(["bash", "-c", self.workflow_steps()[name]], cwd=self.root, env=env,
                                    capture_output=True, text=True)
            if result.returncode:
                break
        return result

    def test_workflow_shell_syntax(self):
        for name, script in self.workflow_steps().items():
            with self.subTest(step=name):
                result = subprocess.run(["bash", "-n"], input=script, capture_output=True, text=True)
                self.assertEqual(0, result.returncode, result.stderr)

    def test_workflow_upload_archive_and_publish_last(self):
        out, m = self.build([self.source()])
        result = self.run_workflow(out, ["Create fixed data release if needed", "Upload immutable data asset",
                                         "Archive immutable manifest for rollback", "Publish stable manifest LAST"])
        self.assertEqual(0, result.returncode, result.stderr)
        remote = self.root / "remote"
        self.assertEqual(m, json.loads((remote / OUTPUT_MANIFEST).read_text()))
        self.assertEqual(1, len(list(remote.glob("taiwan-new-books-manifest-*.json"))))
        operations = [json.loads(line) for line in (self.root / "gh-log").read_text().splitlines()]
        uploads = [op for op in operations if op[1] == "upload"]
        self.assertEqual(OUTPUT_MANIFEST, Path(uploads[-1][-1]).name)
        self.assertNotIn('--clobber', uploads[0])

    def test_workflow_archives_old_pointer_and_keeps_old_data(self):
        out, old = self.build([self.source()])
        remote = self.root / "remote"; remote.mkdir()
        (remote / '.release').touch()
        shutil.copyfile(out / old['data_asset'], remote / old['data_asset'])
        previous_bytes = json.dumps(old).encode()
        (remote / OUTPUT_MANIFEST).write_bytes(previous_bytes)
        _, new = self.build([self.source("2026-04")])
        result = self.run_workflow(out, ["Compare with published manifest", "Upload immutable data asset",
                                         "Archive immutable manifest for rollback", "Publish stable manifest LAST"])
        self.assertEqual(0, result.returncode, result.stderr)
        archive = remote / ("taiwan-new-books-manifest-" + digest_bytes(previous_bytes) + ".json")
        self.assertEqual(previous_bytes, archive.read_bytes())
        self.assertTrue((remote / old['data_asset']).is_file())
        self.assertEqual(new, json.loads((remote / OUTPUT_MANIFEST).read_text()))

    def test_workflow_data_upload_failure_preserves_stable_manifest(self):
        out, _ = self.build([self.source()])
        remote = self.root / "remote"; remote.mkdir()
        (remote / '.release').touch()
        (remote / OUTPUT_MANIFEST).write_text('previous manifest')
        result = self.run_workflow(out, ["Upload immutable data asset", "Publish stable manifest LAST"], fail_upload=True)
        self.assertNotEqual(0, result.returncode)
        self.assertEqual('previous manifest', (remote / OUTPUT_MANIFEST).read_text())

    def test_workflow_existing_wrong_data_never_clobbers_or_publishes(self):
        out, m = self.build([self.source()])
        remote = self.root / "remote"; remote.mkdir()
        (remote / '.release').touch()
        (remote / m['data_asset']).write_bytes(b'conflicting bytes')
        (remote / OUTPUT_MANIFEST).write_text('previous manifest')
        result = self.run_workflow(out, ["Upload immutable data asset", "Publish stable manifest LAST"])
        self.assertNotEqual(0, result.returncode)
        self.assertEqual(b'conflicting bytes', (remote / m['data_asset']).read_bytes())
        self.assertEqual('previous manifest', (remote / OUTPUT_MANIFEST).read_text())

    def test_workflow_recovers_interrupted_first_publish_without_manifest(self):
        out, m = self.build([self.source()])
        remote = self.root / "remote"; remote.mkdir()
        (remote / '.release').touch()
        shutil.copyfile(out / m['data_asset'], remote / m['data_asset'])
        result = self.run_workflow(out, ["Compare with published manifest"])
        self.assertEqual(0, result.returncode, result.stderr)
        self.assertEqual('changed=true\n', (self.root / 'github-output').read_text())
        result = self.run_workflow(out, ["Upload immutable data asset", "Archive immutable manifest for rollback",
                                         "Publish stable manifest LAST"])
        self.assertEqual(0, result.returncode, result.stderr)
        self.assertTrue((remote / OUTPUT_MANIFEST).is_file())

    def test_workflow_unchanged_content_skips_publish(self):
        out, m = self.build([self.source()])
        remote = self.root / "remote"; remote.mkdir()
        (remote / '.release').touch()
        (remote / OUTPUT_MANIFEST).write_text(json.dumps(m))
        result = self.run_workflow(out, ["Compare with published manifest"])
        self.assertEqual(0, result.returncode, result.stderr)
        self.assertEqual('changed=false\n', (self.root / 'github-output').read_text())

    def test_publication_workflow_orders_immutable_data_before_stable_manifest(self):
        workflow = Path(__file__).resolve().parents[2] / ".github/workflows/build-taiwan-new-books-index.yml"
        text = workflow.read_text(encoding="utf-8")
        data = text.index("- name: Upload immutable data asset")
        manifest = text.index("- name: Publish stable manifest LAST")
        self.assertLess(data, manifest)
        self.assertNotIn("--clobber", text[data:manifest])
        self.assertNotIn("gh release delete", text)
        self.assertIn('sha256_file(existing)', text[data:manifest])
        self.assertIn('previous.get("content_version")', text)
        self.assertIn('--clobber', text[manifest:])


if __name__ == "__main__":
    unittest.main()
