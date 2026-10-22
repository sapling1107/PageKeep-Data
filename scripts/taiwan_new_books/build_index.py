#!/usr/bin/env python3
"""Build a standalone, provenance-preserving NCL pre-publication index."""
from __future__ import annotations

import argparse
import csv
from contextlib import closing
import gzip
import hashlib
import io
import json
import os
import re
import shutil
import sqlite3
import ssl
import tempfile
import unicodedata
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from html.parser import HTMLParser
from pathlib import Path

DATASET_NAME = "臺灣出版新書預告書訊"
SOURCE_PAGE = (
    "https://isbn.ncl.edu.tw/NEW_ISBNNet/main_ProcessMenuItems.php"
    "?Pact=ViewContent&Pfld=Ffile&Ptarget=418&Pval=418"
)
LICENSE = {"name": "政府資料開放授權條款第1版", "identifier": "OGDL-1.0",
           "url": "https://data.gov.tw/license"}
ATTRIBUTION = "國家圖書館（National Central Library, Taiwan）— 臺灣出版新書預告書訊；依政府資料開放授權條款第1版利用。"
BUILDER_VERSION = "3"
FORMAT_VERSION = 2
SQLITE_SCHEMA_VERSION = 3
RAW_LAYOUT_VERSION = 1
NORMALIZATION_VERSION = 1
APPLICATION_ID = 1347114062
USER_AGENT = "PageKeep-Taiwan-New-Books-Index/3 (+https://github.com/sapling1107/PageKeep-Data)"
OUTPUT_MANIFEST = "taiwan-new-books-manifest.json"
OFFICIAL_HOSTS = {"isbn.ncl.edu.tw", "www.ncl.edu.tw"}
REQUIRED_HEADERS = {"申請書名", "作者", "出版機構", "預訂出版日", "ISBN", "頁數",
                    "作品語文", "作品語文(其他)", "出版形式"}
COMMON_HYPHENS = "-‐‑‒–—―−－"
PUNCTUATION = set("·・:：,，.。!！?？;；/／\\()（）[]【】「」『』\"“”'‘’")
RAW_FIELDS = {
    "title": "申請書名", "author_raw": "作者", "publisher": "出版機構",
    "publication_date_raw": "預訂出版日", "isbn_raw": "ISBN", "page_count_raw": "頁數",
    "language_raw": "作品語文", "language_other_raw": "作品語文(其他)",
    "edition_raw": "版次", "binding_raw": "裝訂方式", "binding_other_raw": "其他裝訂方式",
    "publication_format_raw": "出版形式",
}
BOOK_COLUMNS = {"id", "source_id", "source_row", "raw_layout_id", "raw_extras_values_json",
                "isbn10", "isbn13", "normalized_isbn", "valid_isbns_json", "page_count",
                "normalized_title", "normalized_author", *RAW_FIELDS}
SOURCE_COLUMNS = {"id", "source_key", "month", "identifier", "url", "sha256", "record_count"}
LAYOUT_COLUMNS = {"id", "layout_version", "keys_json"}
RAW_HEADER_TO_COLUMN = {header: column for column, header in RAW_FIELDS.items()}


class BuildError(RuntimeError):
    """Invalid source or generated artifact; fail before publication."""


def canonical(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def digest_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def validate_official_url(url: str) -> str:
    parsed = urllib.parse.urlparse(url)
    if (parsed.scheme != "https" or parsed.hostname not in OFFICIAL_HOSTS
            or parsed.username or parsed.password or parsed.port not in (None, 443)):
        raise BuildError(f"Not an allowlisted official HTTPS URL: {url}")
    return url


def request_bytes(url: str, timeout: int = 60) -> tuple[bytes, str | None, str | None]:
    validate_official_url(url)
    context = ssl.create_default_context()
    # Preserve normal CA and hostname validation; match Python 3.12's NCL chain compatibility.
    if hasattr(ssl, "VERIFY_X509_STRICT"):
        context.verify_flags &= ~ssl.VERIFY_X509_STRICT

    class OfficialRedirects(urllib.request.HTTPRedirectHandler):
        def redirect_request(self, req, fp, code, msg, headers, newurl):
            validate_official_url(newurl)
            return super().redirect_request(req, fp, code, msg, headers, newurl)

    opener = urllib.request.build_opener(OfficialRedirects(), urllib.request.HTTPSHandler(context=context))
    try:
        request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
        with opener.open(request, timeout=timeout) as response:
            if response.status != 200:
                raise BuildError(f"HTTP {response.status} downloading {url}")
            return response.read(), response.headers.get("Last-Modified"), response.headers.get_content_charset()
    except BuildError:
        raise
    except Exception as error:
        raise BuildError(f"Unable to download {url}: {type(error).__name__}") from error


class MonthlyCsvParser(HTMLParser):
    """Read only explicitly linked year/month resources on the official listing."""
    def __init__(self, base_url: str):
        super().__init__(convert_charrefs=True)
        self.base_url = base_url
        self.year: int | None = None
        self.href: str | None = None
        self.text: list[str] = []
        self.links: set[tuple[str, str]] = set()

    def handle_starttag(self, tag, attrs):
        if tag.lower() == "a":
            self.href = dict(attrs).get("href")
            self.text = []

    def handle_data(self, data):
        year = re.search(r"(20[0-9]{2})\s*年", data)
        if year:
            self.year = int(year.group(1))
        if self.href is not None:
            self.text.append(data)

    def handle_endtag(self, tag):
        if tag.lower() != "a" or self.href is None:
            return
        month = re.fullmatch(r"(1[0-2]|[1-9])\s*月", "".join(self.text).strip())
        url = urllib.parse.urljoin(self.base_url, self.href)
        if self.year and month and urllib.parse.urlparse(url).path.lower().endswith(".csv"):
            validate_official_url(url)
            self.links.add((f"{self.year:04d}-{int(month.group(1)):02d}", url))
        self.href = None
        self.text = []


def discover_sources(source_page: str = SOURCE_PAGE) -> list[dict]:
    body, _, charset = request_bytes(source_page)
    try:
        parser = MonthlyCsvParser(source_page)
        parser.feed(body.decode(charset or "utf-8-sig"))
    except (UnicodeDecodeError, LookupError) as error:
        raise BuildError("Official source listing cannot be decoded") from error
    if not parser.links:
        raise BuildError("No explicit dated official CSV links found; no historical URL guessing")
    return [{"month": month, "url": url, "identifier": url} for month, url in sorted(parser.links)]


def normalize_text(value: str) -> str:
    value = unicodedata.normalize("NFKC", value).lower()
    value = "".join(" " if c in PUNCTUATION or unicodedata.category(c) == "Pd" else c for c in value)
    return " ".join(value.split())


def compact_isbn(value: str) -> str:
    value = unicodedata.normalize("NFKC", value).strip()
    value = re.sub(r"^ISBN(?:-?1[03])?\s*:?\s*", "", value, flags=re.IGNORECASE)
    return "".join(c for c in value if not c.isspace() and c not in COMMON_HYPHENS).upper()


def is_valid_isbn10(value: str) -> bool:
    return bool(re.fullmatch(r"[0-9]{9}[0-9X]", value)) and sum(
        (10 - i) * (10 if c == "X" else int(c)) for i, c in enumerate(value)
    ) % 11 == 0


def is_valid_isbn13(value: str) -> bool:
    return bool(re.fullmatch(r"97[89][0-9]{10}", value)) and sum(
        int(c) * (1 if i % 2 == 0 else 3) for i, c in enumerate(value)
    ) % 10 == 0


def isbn10_to_isbn13(value: str) -> str:
    stem = "978" + value[:9]
    total = sum(int(c) * (1 if i % 2 == 0 else 3) for i, c in enumerate(stem))
    return stem + str((10 - total % 10) % 10)


def normalized_isbns(raw: str) -> list[dict]:
    result = {}
    for part in [raw, *re.split(r"[;,/、\n]", raw)]:
        value = compact_isbn(part)
        if is_valid_isbn10(value):
            result[value] = {"isbn10": value, "isbn13": isbn10_to_isbn13(value)}
        elif is_valid_isbn13(value):
            result[value] = {"isbn10": None, "isbn13": value}
    return [result[key] for key in sorted(result)]


def parse_isbns(raw: str) -> tuple[str | None, str | None, str | None]:
    values = normalized_isbns(raw)
    if not values:
        return None, None, None
    primary = values[0]["isbn13"]
    isbn10 = next((v["isbn10"] for v in values if v["isbn13"] == primary and v["isbn10"]), None)
    explicit13 = primary if any(compact_isbn(p) == primary for p in re.split(r"[;,/、\n]", raw)) else None
    return isbn10, explicit13, primary


def parse_page_count(raw: str) -> int | None:
    value = unicodedata.normalize("NFKC", raw).strip()
    return int(value) if re.fullmatch(r"[0-9]+", value) and int(value) > 0 else None


def timestamp() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def build_timestamp(explicit: str | None) -> str:
    if explicit:
        return explicit
    if "SOURCE_DATE_EPOCH" in os.environ:
        return datetime.fromtimestamp(int(os.environ["SOURCE_DATE_EPOCH"]), timezone.utc).isoformat().replace("+00:00", "Z")
    return timestamp()


def load_sources_config(path: Path) -> list[dict]:
    try:
        resources = json.loads(path.read_text(encoding="utf-8"))["sources"]
        for resource in resources:
            if "path" in resource:
                resource["path"] = path.parent / resource["path"]
        return resources
    except (ValueError, KeyError, TypeError) as error:
        raise BuildError("Invalid sources config; expected a sources array") from error


def read_sources(resources: list[dict]) -> tuple[list[dict], list[dict]]:
    if not resources:
        raise BuildError("At least one explicit source is required")
    sources, books, seen = [], [], set()
    for resource in resources:
        month = resource.get("month", "")
        if not re.fullmatch(r"[0-9]{4}-(0[1-9]|1[0-2])", month):
            raise BuildError("Each source requires an explicit YYYY-MM resource month")
        url = validate_official_url(resource.get("url", ""))
        identifier = resource.get("identifier", url)
        if not isinstance(identifier, str) or not identifier.strip():
            raise BuildError("Source identifier must be non-empty text")
        if "path" in resource:
            if not re.fullmatch(r"[0-9a-f]{64}", resource.get("sha256", "")):
                raise BuildError("Local source files require a pinned SHA-256")
            body = Path(resource["path"]).read_bytes()
            retrieved_at, last_modified = resource.get("retrieved_at"), resource.get("last_modified")
        else:
            body, last_modified, _ = request_bytes(url)
            retrieved_at = timestamp()
        digest = digest_bytes(body)
        if resource.get("sha256") and resource["sha256"] != digest:
            raise BuildError(f"Source SHA-256 mismatch: {identifier}")
        descriptor = {"month": month, "identifier": identifier, "url": url, "sha256": digest}
        source_key = digest_bytes(canonical(descriptor).encode())
        if source_key in seen:
            raise BuildError("Duplicate source resource; list each resource once")
        seen.add(source_key)
        source = {**descriptor, "source_key": source_key, "retrieved_at": retrieved_at,
                  "last_modified": last_modified, "record_count": 0}
        try:
            if not body or b"<html" in body[:1024].lower() or b"<!doctype html" in body[:1024].lower():
                raise BuildError("Empty or HTML source instead of CSV")
            reader = csv.reader(io.StringIO(body.decode("utf-8-sig"), newline=""), strict=True)
            headers = next(reader, [])
            if (len(headers) != len(set(headers)) or not REQUIRED_HEADERS.issubset(headers)):
                raise BuildError("Missing/duplicate CSV headers; unexpected schema")
            for row_number, values in enumerate(reader, start=2):
                if not values or all(not v.strip() for v in values):
                    continue
                if len(values) != len(headers):
                    raise BuildError(f"CSV row {row_number} has wrong column count")
                raw = dict(zip(headers, values))
                if not raw["申請書名"].strip():
                    raise BuildError(f"CSV row {row_number} has blank title")
                isbn10, isbn13, normalized = parse_isbns(raw["ISBN"])
                books.append({"source_key": source_key, "source_row": row_number, "_raw_record": raw,
                              **{field: raw.get(header, "") for field, header in RAW_FIELDS.items()},
                              "isbn10": isbn10, "isbn13": isbn13, "normalized_isbn": normalized,
                              "valid_isbns_json": canonical(normalized_isbns(raw["ISBN"])),
                              "page_count": parse_page_count(raw["頁數"]),
                              "normalized_title": normalize_text(raw["申請書名"]),
                              "normalized_author": normalize_text(raw["作者"])})
                source["record_count"] += 1
        except (UnicodeDecodeError, csv.Error) as error:
            raise BuildError(f"Invalid UTF-8 CSV: {identifier}") from error
        if not source["record_count"]:
            raise BuildError(f"Source contains no records: {identifier}")
        sources.append(source)
    sources.sort(key=lambda s: (s["month"], s["identifier"], s["url"], s["sha256"]))
    books.sort(key=lambda b: (b["source_key"], b["source_row"]))
    return sources, books


def source_identity(source: dict) -> dict:
    return {key: source[key] for key in ("month", "identifier", "url", "sha256")}


def content_version(sources: list[dict]) -> str:
    contract = {"format_version": FORMAT_VERSION, "sqlite_schema_version": SQLITE_SCHEMA_VERSION,
                "normalization_version": NORMALIZATION_VERSION, "builder_version": BUILDER_VERSION,
                "raw_layout_version": RAW_LAYOUT_VERSION, "unicode_version": unicodedata.unidata_version,
                "sources": sorted((source_identity(s) for s in sources), key=canonical)}
    return digest_bytes(canonical(contract).encode())


def coverage(sources: list[dict]) -> dict:
    months = sorted({s["month"] for s in sources})
    return {"coverage_start": months[0], "coverage_end": months[-1], "coverage_months": months,
            "coverage_scope": "explicit_resources_only_not_complete_national_bibliography"}


def decode_layout(layout: dict) -> list[str]:
    keys = json.loads(layout["keys_json"])
    if (layout["layout_version"] != RAW_LAYOUT_VERSION or not isinstance(keys, list)
            or not all(isinstance(key, str) for key in keys) or keys != sorted(set(keys))
            or not REQUIRED_HEADERS.issubset(keys)):
        raise BuildError("Invalid raw layout")
    return keys


def reconstruct_raw_record(book: dict, layout: dict, extras: str | list | None = None) -> dict[str, str]:
    """Reconstruct exact CSV header/value membership, without normalizing raw values."""
    keys = decode_layout(layout)
    if extras is None:
        extras = book["raw_extras_values_json"]
    values = json.loads(extras) if isinstance(extras, str) else extras
    extra_keys = [key for key in keys if key not in RAW_HEADER_TO_COLUMN]
    if (not isinstance(values, list) or len(values) != len(extra_keys)
            or not all(isinstance(value, str) for value in values)):
        raise BuildError("Invalid raw extras positional array")
    raw = dict(zip(extra_keys, values))
    for key in keys:
        if key in RAW_HEADER_TO_COLUMN:
            value = book[RAW_HEADER_TO_COLUMN[key]]
            if not isinstance(value, str):
                raise BuildError("Mapped raw value is not text")
            raw[key] = value
    return raw


def raw_layouts(books: list[dict]) -> list[dict]:
    # Sort full key sets, including optional/unknown/blank names. Missing != empty.
    key_sets = sorted({tuple(sorted(book["_raw_record"])) for book in books})
    return [{"id": index, "layout_version": RAW_LAYOUT_VERSION, "keys_json": canonical(keys)}
            for index, keys in enumerate(key_sets, start=1)]


def verify_source_rows(connection: sqlite3.Connection, books: list[dict]) -> dict:
    """Mandatory full source-to-SQLite equality/provenance check before publication."""
    layouts = {row[0]: {"id": row[0], "layout_version": row[1], "keys_json": row[2]}
               for row in connection.execute("SELECT id,layout_version,keys_json FROM raw_layouts")}
    sources = dict(connection.execute("SELECT id,source_key FROM sources"))
    cursor = connection.execute("SELECT * FROM books ORDER BY id")
    names = [column[0] for column in cursor.description]
    checked = 0
    for expected in books:
        row = cursor.fetchone()
        if row is None:
            raise BuildError(f"Missing occurrence at expected row {checked + 1}")
        actual = dict(zip(names, row))
        if (sources.get(actual["source_id"]) != expected["source_key"]
                or actual["source_row"] != expected["source_row"]):
            raise BuildError(f"Provenance mismatch at built row {actual['id']}")
        raw = reconstruct_raw_record(actual, layouts[actual["raw_layout_id"]])
        if canonical(raw) != canonical(expected["_raw_record"]):
            fields = sorted(key for key in raw.keys() | expected["_raw_record"].keys()
                            if key not in raw or key not in expected["_raw_record"]
                            or raw[key] != expected["_raw_record"][key])
            raise BuildError(f"Raw round-trip mismatch at source row {actual['source_row']}; fields={fields}")
        checked += 1
    if cursor.fetchone() is not None:
        raise BuildError("Unexpected extra source occurrence")
    return {"rows_checked": checked, "mismatch_count": 0, "sources_checked": len(sources)}


def create_database(path: Path, sources: list[dict], books: list[dict], version: str) -> dict:
    layouts = raw_layouts(books)
    layout_ids = {layout["keys_json"]: layout["id"] for layout in layouts}
    source_ids = {source["source_key"]: index for index, source in enumerate(sources, start=1)}
    with closing(sqlite3.connect(path)) as connection, connection:
        connection.executescript(f"""
            PRAGMA page_size=4096;
            PRAGMA journal_mode=DELETE;
            PRAGMA application_id={APPLICATION_ID};
            PRAGMA user_version={SQLITE_SCHEMA_VERSION};
            PRAGMA foreign_keys=ON;
            CREATE TABLE metadata (key TEXT PRIMARY KEY NOT NULL, value TEXT NOT NULL) WITHOUT ROWID;
            CREATE TABLE sources (
                id INTEGER PRIMARY KEY, source_key TEXT NOT NULL, month TEXT NOT NULL,
                identifier TEXT NOT NULL, url TEXT NOT NULL, sha256 TEXT NOT NULL,
                record_count INTEGER NOT NULL
            );
            CREATE TABLE raw_layouts (
                id INTEGER PRIMARY KEY, layout_version INTEGER NOT NULL, keys_json TEXT NOT NULL
            );
            CREATE TABLE books (
                id INTEGER PRIMARY KEY, source_id INTEGER NOT NULL REFERENCES sources(id),
                source_row INTEGER NOT NULL, raw_layout_id INTEGER NOT NULL REFERENCES raw_layouts(id),
                raw_extras_values_json TEXT NOT NULL,
                {','.join(field + ' TEXT NOT NULL' for field in RAW_FIELDS)},
                isbn10 TEXT, isbn13 TEXT, normalized_isbn TEXT, valid_isbns_json TEXT NOT NULL,
                page_count INTEGER CHECK(page_count IS NULL OR page_count > 0),
                normalized_title TEXT NOT NULL, normalized_author TEXT NOT NULL,
                UNIQUE(source_id, source_row)
            );
            CREATE INDEX books_normalized_isbn_idx ON books(normalized_isbn);
            CREATE INDEX books_normalized_title_idx ON books(normalized_title);
            CREATE INDEX books_normalized_author_idx ON books(normalized_author);
        """)
        metadata = {"builder_version": BUILDER_VERSION, "dataset": DATASET_NAME,
                    "schema_version": str(SQLITE_SCHEMA_VERSION), "normalization_version": str(NORMALIZATION_VERSION),
                    "raw_layout_version": str(RAW_LAYOUT_VERSION), "content_version": version,
                    "source_homepage": SOURCE_PAGE, "license": canonical(LICENSE), "attribution": ATTRIBUTION}
        connection.executemany("INSERT INTO metadata VALUES (?,?)", sorted(metadata.items()))
        # Volatile retrieval metadata remains in the complete manifest sources list,
        # joined by source_key, exactly as in v2; it must not alter immutable DB bytes.
        connection.executemany("INSERT INTO sources VALUES (?,?,?,?,?,?,?)", [
            (source_ids[s["source_key"]], *(s[k] for k in
             ("source_key", "month", "identifier", "url", "sha256", "record_count"))) for s in sources])
        connection.executemany("INSERT INTO raw_layouts VALUES (?,?,?)", [
            (layout["id"], layout["layout_version"], layout["keys_json"]) for layout in layouts])
        columns = ["source_id", "source_row", "raw_layout_id", "raw_extras_values_json", *RAW_FIELDS,
                   "isbn10", "isbn13", "normalized_isbn", "valid_isbns_json", "page_count", "normalized_title", "normalized_author"]

        def encoded_rows():
            for book in books:
                raw = book["_raw_record"]
                keys = sorted(raw)
                encoded = {**book, "source_id": source_ids[book["source_key"]],
                           "raw_layout_id": layout_ids[canonical(keys)],
                           "raw_extras_values_json": canonical([raw[key] for key in keys if key not in RAW_HEADER_TO_COLUMN])}
                yield tuple(encoded[column] for column in columns)

        connection.executemany(f"INSERT INTO books ({','.join(columns)}) VALUES ({','.join('?' for _ in columns)})", encoded_rows())
        report = verify_source_rows(connection, books)
        connection.commit()
        connection.execute("VACUUM")
        return report


def validate_outputs(output_dir: Path) -> None:
    manifest = json.loads((output_dir / OUTPUT_MANIFEST).read_text(encoding="utf-8"))
    expected = {"format_version": FORMAT_VERSION, "sqlite_schema_version": SQLITE_SCHEMA_VERSION,
                "normalization_version": NORMALIZATION_VERSION, "builder_version": BUILDER_VERSION,
                "raw_layout_version": RAW_LAYOUT_VERSION, "unicode_version": unicodedata.unidata_version,
                "dataset": DATASET_NAME, "source_homepage": SOURCE_PAGE, "license": LICENSE, "attribution": ATTRIBUTION}
    if any(manifest.get(k) != v for k, v in expected.items()):
        raise BuildError("Manifest contract mismatch")
    sources = manifest["sources"]
    if manifest.get("source_round_trip") != {"rows_checked": manifest["record_count"], "mismatch_count": 0, "sources_checked": len(sources)}:
        raise BuildError("Manifest source round-trip report mismatch")
    version = content_version(sources)
    if manifest["content_version"] != version or any(manifest.get(k) != v for k, v in coverage(sources).items()):
        raise BuildError("Content version/coverage mismatch")
    sqlite_name = f"taiwan-new-books-{version}.sqlite"
    gzip_name = sqlite_name + ".gz"
    if manifest["sqlite_asset"] != sqlite_name or manifest["data_asset"] != gzip_name:
        raise BuildError("Unsafe or inconsistent asset filename")
    sqlite_path, gzip_path = output_dir / sqlite_name, output_dir / gzip_name
    for path, size_key, hash_key in ((sqlite_path, "uncompressed_size", "sqlite_sha256"),
                                     (gzip_path, "compressed_size", "gzip_sha256")):
        if path.stat().st_size != manifest[size_key] or sha256_file(path) != manifest[hash_key]:
            raise BuildError(f"Hash/size mismatch: {path.name}")
    with sqlite_path.open("rb") as stream:
        if stream.read(16) != b"SQLite format 3\x00":
            raise BuildError("Invalid SQLite header")
    try:
        if gzip.decompress(gzip_path.read_bytes()) != sqlite_path.read_bytes():
            raise BuildError("gzip payload differs from SQLite")
    except (OSError, EOFError) as error:
        raise BuildError("Invalid gzip") from error
    with closing(sqlite3.connect(sqlite_path.resolve().as_uri() + "?mode=ro&immutable=1", uri=True)) as connection:
        if connection.execute("PRAGMA application_id").fetchone()[0] != APPLICATION_ID:
            raise BuildError("Wrong SQLite application ID")
        if connection.execute("PRAGMA user_version").fetchone()[0] != SQLITE_SCHEMA_VERSION:
            raise BuildError("Wrong SQLite schema version")
        if connection.execute("PRAGMA integrity_check").fetchall() != [("ok",)]:
            raise BuildError("SQLite integrity failure")
        if connection.execute("PRAGMA foreign_key_check").fetchall():
            raise BuildError("SQLite foreign key failure")
        for table, required in (("metadata", {"key", "value"}), ("sources", SOURCE_COLUMNS), ("raw_layouts", LAYOUT_COLUMNS), ("books", BOOK_COLUMNS)):
            columns = {row[1] for row in connection.execute(f"PRAGMA table_info({table})")}
            if not required.issubset(columns):
                raise BuildError(f"Missing required table/columns: {table}")
        metadata = dict(connection.execute("SELECT key,value FROM metadata"))
        if metadata.get("content_version") != version or metadata.get("normalization_version") != str(NORMALIZATION_VERSION):
            raise BuildError("SQLite metadata mismatch")
        count = connection.execute("SELECT COUNT(*) FROM books").fetchone()[0]
        if count <= 0 or count != manifest["record_count"] or count != sum(s["record_count"] for s in sources):
            raise BuildError("Record count mismatch")
        actual_sources = connection.execute("SELECT source_key,month,identifier,url,sha256,record_count FROM sources ORDER BY source_key").fetchall()
        expected_sources = sorted(tuple(s[k] for k in ("source_key", "month", "identifier", "url", "sha256", "record_count")) for s in sources)
        if actual_sources != expected_sources:
            raise BuildError("Source provenance mismatch")
        for source_id, record_count in connection.execute("SELECT id,record_count FROM sources"):
            if connection.execute("SELECT COUNT(*) FROM books WHERE source_id=?", (source_id,)).fetchone()[0] != record_count:
                raise BuildError("Per-source record count mismatch")
        for table in ("books", "sources", "raw_layouts"):
            info = {row[1]: row for row in connection.execute(f"PRAGMA table_info({table})")}
            if info["id"][2].upper() != "INTEGER" or info["id"][5] != 1:
                raise BuildError(f"Expected integer internal primary key: {table}")
        columns = {row[1]: row[2].upper() for row in connection.execute("PRAGMA table_info(books)")}
        if ({"source_identifier", "source_key", "raw_record_json"} & columns.keys()
                or any(columns[k] != "INTEGER" for k in ("source_id", "source_row", "raw_layout_id"))):
            raise BuildError("Forbidden legacy hot-row identity or wrong integer reference")
        foreign_keys = {(row[3], row[2], row[4]) for row in connection.execute("PRAGMA foreign_key_list(books)")}
        if foreign_keys != {("source_id", "sources", "id"), ("raw_layout_id", "raw_layouts", "id")}:
            raise BuildError("Missing expected source/layout foreign key declaration")
        index_rows = connection.execute("PRAGMA index_list(books)").fetchall()
        index_columns = {row[1]: tuple(col[2] for col in connection.execute(f'PRAGMA index_info("{row[1]}")')) for row in index_rows}
        expected_indexes = {"books_normalized_isbn_idx": ("normalized_isbn",),
                            "books_normalized_title_idx": ("normalized_title",),
                            "books_normalized_author_idx": ("normalized_author",)}
        if any(index_columns.get(name) != columns for name, columns in expected_indexes.items()):
            raise BuildError("Required search B-tree missing or malformed")
        if (len(index_rows) != 4 or not any(row[2] and index_columns[row[1]] == ("source_id", "source_row") for row in index_rows)):
            raise BuildError("Expected only three search indexes and UNIQUE(source_id,source_row)")
        if connection.execute("SELECT count(*) FROM sqlite_master WHERE upper(sql) LIKE '%CREATE VIRTUAL TABLE%'").fetchone()[0]:
            raise BuildError("Virtual tables/FTS are outside this contract")
        layouts = {row[0]: {"id": row[0], "layout_version": row[1], "keys_json": row[2]}
                   for row in connection.execute("SELECT id,layout_version,keys_json FROM raw_layouts")}
        layout_keys = [tuple(decode_layout(layout)) for layout in layouts.values()]
        if (len(set(layout_keys)) != len(layout_keys) or not layouts
                or sorted(layouts) != list(range(1, len(layouts) + 1))
                or [tuple(decode_layout(layouts[index])) for index in sorted(layouts)] != sorted(layout_keys)):
            raise BuildError("Duplicate, empty or nondeterministically numbered raw layouts")
        if metadata.get("raw_layout_version") != str(RAW_LAYOUT_VERSION):
            raise BuildError("Raw layout metadata mismatch")
        cursor = connection.execute("SELECT * FROM books ORDER BY id")
        names = [column[0] for column in cursor.description]
        used_layouts = set()
        for row in cursor:
            book = dict(zip(names, row))
            if any(type(book[key]) is not int or book[key] <= 0 for key in ("id", "source_id", "source_row", "raw_layout_id")):
                raise BuildError("Invalid internal integer identity/ordinal")
            raw = reconstruct_raw_record(book, layouts[book["raw_layout_id"]])
            if book["source_row"] < 2 or any(book[column] != "" for column, key in RAW_FIELDS.items() if key not in raw):
                raise BuildError("Invalid source ordinal or nonempty absent raw field")
            used_layouts.add(book["raw_layout_id"])
            if (book["normalized_title"] != normalize_text(raw["申請書名"])
                    or book["normalized_author"] != normalize_text(raw["作者"])):
                raise BuildError("Normalized search fields differ from reconstructed raw values")
        if used_layouts != layouts.keys():
            raise BuildError("Unused raw layout")


def build_index(output_dir: Path, resources: list[dict] | None = None,
                source_page: str = SOURCE_PAGE, built_at: str | None = None) -> dict:
    sources, books = read_sources(discover_sources(source_page) if resources is None else resources)
    version = content_version(sources)
    sqlite_name = f"taiwan-new-books-{version}.sqlite"
    output_dir.mkdir(parents=True, exist_ok=True)
    # Same filesystem staging; manifest is the last file replaced locally too.
    with tempfile.TemporaryDirectory(prefix=".index-", dir=output_dir) as temporary:
        staged = Path(temporary)
        sqlite_path, gzip_path = staged / sqlite_name, staged / (sqlite_name + ".gz")
        round_trip = create_database(sqlite_path, sources, books, version)
        with sqlite_path.open("rb") as src, gzip_path.open("wb") as dst:
            with gzip.GzipFile(filename="", mode="wb", fileobj=dst, mtime=0) as compressed:
                shutil.copyfileobj(src, compressed)
        manifest = {"format_version": FORMAT_VERSION, "sqlite_schema_version": SQLITE_SCHEMA_VERSION,
                    "normalization_version": NORMALIZATION_VERSION, "builder_version": BUILDER_VERSION,
                    "unicode_version": unicodedata.unidata_version, "content_version": version,
                    "built_at": build_timestamp(built_at), "dataset": DATASET_NAME,
                    "source_homepage": SOURCE_PAGE, "license": LICENSE, "attribution": ATTRIBUTION,
                    "sources": sources, **coverage(sources), "record_count": len(books),
                    "raw_layout_version": RAW_LAYOUT_VERSION, "source_round_trip": round_trip,
                    "sqlite_asset": sqlite_path.name, "data_asset": gzip_path.name,
                    "uncompressed_size": sqlite_path.stat().st_size, "compressed_size": gzip_path.stat().st_size,
                    "sqlite_sha256": sha256_file(sqlite_path), "gzip_sha256": sha256_file(gzip_path)}
        (staged / OUTPUT_MANIFEST).write_text(json.dumps(manifest, ensure_ascii=False, sort_keys=True, indent=2) + "\n", encoding="utf-8")
        validate_outputs(staged)
        # Immutable content names must never acquire different bytes.
        for path in (sqlite_path, gzip_path):
            destination = output_dir / path.name
            if destination.exists():
                if sha256_file(destination) != sha256_file(path):
                    raise BuildError("Immutable asset conflict; bump builder/contract version")
            else:
                os.replace(path, destination)
        os.replace(staged / OUTPUT_MANIFEST, output_dir / OUTPUT_MANIFEST)
    validate_outputs(output_dir)
    return manifest


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output-dir", type=Path, default=Path("build/taiwan-new-books-index"))
    parser.add_argument("--sources", type=Path, help="Explicit source config; relative local paths resolve beside config")
    parser.add_argument("--source-page", default=SOURCE_PAGE, help="Enumerate only explicit official month links")
    parser.add_argument("--built-at", help="Override build timestamp; never changes data identity")
    args = parser.parse_args()
    try:
        manifest = build_index(args.output_dir, load_sources_config(args.sources) if args.sources else None,
                               args.source_page, args.built_at)
    except (BuildError, OSError, ValueError, sqlite3.Error, KeyError, TypeError) as error:
        print(f"error: {error}")
        return 1
    print(f"Built {manifest['record_count']} records; months={','.join(manifest['coverage_months'])}; "
          f"content_version={manifest['content_version']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
