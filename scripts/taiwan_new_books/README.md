# Taiwan New Books standalone index

Standard-library Python pipeline for National Central Library (NCL), Taiwan,
**臺灣出版新書預告書訊** (government dataset 6730). These are **pre-publication
ISBN application records**, not a complete national bibliography. Resource months
are export/application periods, not the publication dates of every book in them.
Dates may be scheduled dates, author strings may include unparsed role text, and
no cover is supplied. No commercial bookstore scraping or unofficial mirror is used.

- Official source homepage: <https://isbn.ncl.edu.tw/NEW_ISBNNet/main_ProcessMenuItems.php?Pact=ViewContent&Pfld=Ffile&Ptarget=418&Pval=418>
- Dataset: <https://data.gov.tw/dataset/6730>
- Agency: 國家圖書館（National Central Library, Taiwan）
- License: 政府資料開放授權條款第1版 / OGDL-1.0, <https://data.gov.tw/license>

Redistribution and derived indexes must retain the agency/dataset/version and OGDL
attribution/license notice. The manifest and SQLite metadata carry them; a future
app integration must expose attribution. Do not imply NCL endorsement.

The index is a standalone SQLite file intended for read-only consumers. This public
repository contains only the data pipeline and its tests; it does not contain the
PageKeep application source or require access to a private repository.

## Inputs and verified coverage

```sh
python3 scripts/taiwan_new_books/build_index.py
```

Default mode fetches the official source homepage and includes **all explicit dated
CSV links it finds**, rather than only the latest month. It never constructs
historical filenames. Only HTTPS on `isbn.ncl.edu.tw` and `www.ncl.edu.tw` is allowed,
including redirects. Each listed source must download and validate successfully;
a failed resource fails the whole build instead of silently reducing coverage.
HTML parsing here discovers the government dataset's download links; it does not
extract bookstore metadata. CSV is decoded strictly as UTF-8, accepting a BOM.
Unknown columns are preserved; missing required or duplicate headers, malformed
row widths, blank titles, invalid encoding and empty resources fail closed.

For an explicit, reproducible subset or offline fixtures:

```sh
python3 scripts/taiwan_new_books/build_index.py --sources /absolute/path/sources.json \
  --output-dir build/taiwan-new-books-index --built-at 2026-10-22T00:00:00Z
```

The config shape is:

```json
{
  "sources": [
    {
      "month": "YYYY-MM",
      "identifier": "official resource URL or documented source identifier",
      "url": "verified official HTTPS CSV URL",
      "path": "optional local CSV path, relative to this config",
      "sha256": "required 64-character lowercase digest for local files; optional pin for downloads",
      "retrieved_at": "optional timestamp of the original retrieval",
      "last_modified": "optional original HTTP Last-Modified"
    }
  ]
}
```

This is a shape example, not a usable archive manifest. Do not invent official URLs
or resource months. A local file's pinned hash verifies its bytes, **not** its official
provenance: config authors must independently verify the URL/month association.
Synthetic test fixtures are not claimed as downloaded official data. Network mode
records the actual retrieval timestamp and source SHA-256. Local paths never enter
the published manifest. Duplicate resource entries are rejected.

`coverage_start`/`coverage_end` are the min/max **included resource months**;
`coverage_months` is the exact list and may have gaps. Neither endpoint implies
all intervening months, all historical resources, all revisions or all Taiwanese
books. `coverage_scope` explicitly disclaims a complete national bibliography.
The complete included `sources` list is authoritative. Missing months or resources
not exposed on the official listing need separate verification; no archive is guessed.

## Preservation and SQLite schema 3

Tables:

- `metadata(key,value)`: builder/schema/normalization/raw-layout/content versions,
  dataset, homepage, license and attribution.
- `sources(id INTEGER PRIMARY KEY,source_key,month,identifier,url,sha256,record_count)`:
  compact integer identity plus the complete stable source descriptor. The source
  hash is retained once per resource, not repeated in each book. Retrieval and
  Last-Modified observations remain in the manifest's complete `sources` list,
  joined by `source_key`, exactly as in v2. They are not discarded or included in
  immutable SQLite bytes; a new retrieval must not change the data identity.
- `raw_layouts(id INTEGER PRIMARY KEY,layout_version,keys_json)`: unique full raw
  key sets, sorted without renaming, trimming or semantic aliasing. Layout v1 uses
  canonical UTF-8 JSON arrays of keys; key sets and integer IDs are deterministic
  regardless of input source ordering. Empty header names are valid layout members.
- `books(id INTEGER PRIMARY KEY,source_id,source_row,raw_layout_id,raw_extras_values_json,...)`:
  integer references to `sources` and `raw_layouts`, all existing useful raw/search
  fields, and `UNIQUE(source_id,source_row)`.

There is no per-book `source_identifier` hash, string `source_key`, or full
`raw_record_json`, and no index for the removed generated hash. Integer book/source/
layout IDs are internal to one built index, **not official IDs, stable cross-version
IDs or edition identities**. Book IDs follow deterministic source-snapshot/row order;
source IDs follow sorted source descriptors; layout IDs follow sorted full key sets.

### Lossless raw reconstruction (layout version 1)

For each complete layout, extras keys are its sorted keys excluding those mapped
by `RAW_FIELDS`. A book's `raw_extras_values_json` is the canonical positional JSON
array of those extras' original string values, in exactly that order. Key names are
stored once per layout. Mapped raw columns are not removed: title, author, publisher,
ISBN, scheduled date, page count, language/other language, edition, binding/other
binding and publication format all remain independently available.

`reconstruct_raw_record(book, layout, extras)` combines only the keys in that layout
with their mapped-column or extras-array value. Thus an absent optional header
stays absent even when the corresponding typed column defaults to an empty string;
a present empty value stays `""`; a blank header and unknown headers survive exactly.
Differently named official fields (for example `常用分類`/`建議上架分類` and
`是否為翻譯書`/`是否為引進版權著作`) remain distinct. No raw value is trimmed or
normalized, and no speculative author-role/language/publication-format parsing occurs.

Every build compares every reconstructed canonical raw object and source occurrence
against the complete input CSV records **before compression or publication**. The
manifest's `source_round_trip` reports rows checked, mismatches (must be zero) and
source count. This is a build-time source comparison, not a claim that a later
hash-only artifact check can independently prove source equality.

**No bibliographic or payload deduplication is performed.** Every occurrence,
including exact duplicates, keeps its source and 1-based CSV record ordinal
(`source_row`, header=1; not a physical line number for multiline records).
Record count counts occurrences, not unique books or editions.

Dates stay raw: `2026-03-00` retains its unknown day; no parsed date or fake day is
introduced. Page count parsing remains unchanged. SQLite uses
`application_id=1347114062`, `user_version=3`, three normalized ISBN/title/author
B-trees, and the source/row uniqueness constraint. No FTS5, payload hash index,
`book_sources` table or extra speculative indexes. Multi-ISBN information remains
in `valid_isbns_json`; primary normalized ISBN is not edition identity.

## Normalization version 1

Portable input/output cases: `normalization_fixtures.json`, intended for later Kotlin
tests without modifying Kotlin now.

- Text: Unicode NFKC, Unicode lowercase (not casefold), replace Unicode `Pd` dashes
  and the explicit punctuation set in `build_index.py` with spaces, collapse Unicode
  whitespace and trim. No Traditional/Simplified conversion, accent removal, word
  segmentation or fuzzy matching. Raw fields are never replaced by search text.
- ISBN: NFKC; optional case-insensitive ISBN/ISBN10/ISBN13/ISBN-10/ISBN-13 prefix;
  strip whitespace and the documented `COMMON_HYPHENS`; uppercase X; validate ASCII
  ISBN-10 checksum or 978/979 ISBN-13 checksum. Convert ISBN-10 to ISBN-13 comparison
  key. Invalid identifiers stay in `isbn_raw`, with no valid normalized key.
- Multiple ISBN candidates are split on `; , / 、` and newline; all valid normalized
  pairs are retained. A primary pair is selected deterministically and consistently.

This is the external-index normalization contract. Consumers should use the portable
fixtures as their compatibility baseline, including Unicode whitespace and strict
ISBN-13 prefixes. `unicode_version` records the Python
Unicode tables and participates in content identity. Any output-affecting algorithm
change requires a builder/normalization version bump as appropriate.

## Manifest format 2 and artifacts

Default output directory: `build/taiwan-new-books-index/` (ignored build output).

- `taiwan-new-books-<content_version>.sqlite`: local uncompressed artifact.
- `taiwan-new-books-<content_version>.sqlite.gz`: immutable published data asset.
- `taiwan-new-books-manifest.json`: stable manifest, replaced last.

Manifest fields include `format_version`, `sqlite_schema_version`,
`normalization_version`, `builder_version`, `unicode_version`, `content_version`,
`built_at`, `record_count`, `uncompressed_size`, `compressed_size`, `sqlite_sha256`,
`gzip_sha256`, `sqlite_asset`, `data_asset`, coverage fields, complete `sources`,
license, attribution, dataset, source homepage, `raw_layout_version` and
`source_round_trip`. Manifest format remains 2; SQLite schema and builder advance
to 3 while normalization remains 1. Each source includes month,
identifier, official URL, SHA-256, source key, record count and optional retrieval/
Last-Modified timestamps. The old ambiguous `sha256` and fixed-name gzip/sidecar
contract is retired; hashes are explicit manifest fields.

`content_version` is SHA-256 over canonical sorted source descriptors (including
source bytes' SHA-256), format/schema/normalization/builder/Unicode/raw-layout versions.
Input list order, local file paths, retrieval times, Last-Modified and `built_at`
do not affect identity or SQLite bytes. Explicit `--built-at` or `SOURCE_DATE_EPOCH`
can make manifest timestamps reproducible. Gzip uses mtime=0 and no source filename.
Equivalent input resources produce identical bytes in the same pinned runtime;
SQLite/zlib runtime differences can affect binary encoding. A same-version byte
conflict fails closed; never overwrite an immutable asset or silently change its hash.

Validation checks SQLite header, application ID, user_version, integrity, foreign
keys, required tables/columns, total/per-source counts, source descriptors, content
version/coverage, integer keys, layouts, extras shapes, all three search indexes and
source-row uniqueness, both hashes/sizes, and gzip-to-SQLite byte equality.
Generated binaries/configs from validation are not committed.

## Publishing and rollback

GitHub Actions retains its manual trigger and twice-monthly schedule, pinned Python
3.12, fixed Release tag `taiwan-new-books-index` and serialized publishing group.
It runs the full Python suite and builds/validates before publication.

1. Compare content version and gzip hash with the published stable manifest.
2. Upload the content-versioned gzip **without clobber** if absent.
3. If it exists (including a rerun after interruption), reuse it only after
   downloading and verifying its size/hash; verify newly uploaded bytes too.
4. Archive and read back both the incoming and previous stable manifests as immutable
   `taiwan-new-books-manifest-<manifest SHA-256>.json` for rollback.
5. Publish/update the stable manifest **LAST**, in a separate command.
6. Never delete previous data or archived manifests. Operators can restore a prior
   archived manifest as the stable pointer without reuploading old data.

No multi-file atomic Release transaction is assumed. Failure before step 5 leaves
an existing stable manifest untouched; an orphan new data asset is harmless.
GitHub's stable-manifest clobber itself can temporarily remove that manifest if
upload fails: clients must retain the last valid downloaded index and retry later.
The pointer never references a not-yet-uploaded asset. Local validation does not publish
anything. Consumers must reject unsupported
versions or hash/size mismatches and preserve its previous validated index.

## Verified 20-month corpus and actual sizes

The pinned official source files verified on 2026-10-22 contain 102,405 occurrences
across all 20 resource months from **2025-01 through 2026-08**. No earlier/later
historical resources are claimed. There are three full key layouts (27, 27, 28 keys),
captured without book records in `raw_layout_fixtures.json` for portable regression
coverage. First source retrieval metadata and SHA-256 are retained in the manifest.

Actual local artifact (Python Unicode tables 16.0.0, SQLite 3.53.0):

| | v2 | v3 | Reduction |
| --- | ---: | ---: | ---: |
| SQLite | 177,483,776 bytes / 169.26 MiB | 66,310,144 bytes / 63.24 MiB | 111,173,632 bytes / 62.64% |
| gzip | 30,796,424 bytes / 29.37 MiB | 17,350,779 bytes / 16.55 MiB | 13,445,645 bytes / 43.66% |

v3 uses 16,189 pages of 4,096 bytes, freelist=0. Full source-to-v3 and v2-to-v3
canonical raw equality/provenance checks covered **102,405 rows with zero mismatches**,
all 20 resources, all three layouts. Source ordinals are retained exactly.
These are snapshot/runtime measurements, not promised sizes for future datasets.

Equal-size generation storage: first install `D+G` = 83,660,923 bytes / 79.79 MiB;
update with active, previous, new and gzip `3D+G` = 216,281,211 bytes / 206.26 MiB.
Two retained v2-sized generations plus compact new/gzip need 438,628,475 bytes /
418.31 MiB. A future downloader must use actual generation sizes and additional
headroom, not assume the steady compact envelope for its first migration.

## Verification

Run all commands from this repository root:

```sh
python3 -B -m unittest discover -s scripts/taiwan_new_books -p 'test_*.py'
python3 -B -m py_compile scripts/taiwan_new_books/build_index.py scripts/taiwan_new_books/test_build_index.py scripts/taiwan_new_books/verify_index.py
python3 -B scripts/taiwan_new_books/build_index.py
# For a separately pinned, verified official-source snapshot:
python3 -B scripts/taiwan_new_books/build_index.py \
  --sources build/verified-sources/sources.json \
  --output-dir build/taiwan-new-books-index
python3 -B scripts/taiwan_new_books/verify_index.py \
  --sources build/verified-sources/sources.json \
  --output-dir build/taiwan-new-books-index
git diff --check
```

Default mode discovers and validates resources directly from the official listing.
The optional pinned config and CSV files are local inputs, not committed dependencies:
obtain CSVs only from explicit dated links on that listing, verify each URL/month,
record retrieval metadata, save original bytes, and pin their SHA-256 in the config
shape above. Never use invented URLs or substitute synthetic test data for official
coverage. `verify_index.py` checks every source occurrence and reconstructed raw
record against those pinned CSVs; optional `--reference-v2` compares a separately
supplied older schema-v2 artifact, but no older artifact is required to build or test.

The full Python suite is self-contained, including all three verified key layouts,
workflow shell syntax and simulated publication failures. Generated CSV snapshots,
SQLite, gzip and manifests stay in ignored `build/`; they are not source files.

## Public hosting

This repository publishes to its own GitHub Release using the workflow's local
`github.token` (`GH_TOKEN`) with only `contents: write`; `GH_REPO` is explicitly
`${{ github.repository }}`. No PAT, cross-repository token or private repository is
required. The release tag is `taiwan-new-books-index`.

Canonical stable manifest (available after a successful publication):

<https://github.com/sapling1107/PageKeep-Data/releases/download/taiwan-new-books-index/taiwan-new-books-manifest.json>

Append only the validated manifest's `data_asset` filename to that same release
base to download the immutable gzip. Do not use a private application repository's
Release URL. The stable manifest is published last and previous assets are retained.
