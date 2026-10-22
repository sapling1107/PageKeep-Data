# PageKeep Data

Generated public bibliographic indexes used by PageKeep. This repository does **not**
contain the PageKeep application source code and builds independently of it.

## Taiwan New Books

The current pipeline uses **臺灣出版新書預告書訊**, National Central Library (Taiwan)
government open data: pre-publication ISBN bibliographic information, **not a
complete national bibliography**. Resource months describe source exports, not
necessarily each book's publication date. No covers or bookstore data are included.

- [Official dataset](https://data.gov.tw/dataset/6730)
- [Official resource listing](https://isbn.ncl.edu.tw/NEW_ISBNNet/main_ProcessMenuItems.php?Pact=ViewContent&Pfld=Ffile&Ptarget=418&Pval=418)
- [Pipeline, validation and source-snapshot instructions](scripts/taiwan_new_books/README.md)

## Index contract and downloads

- Standalone read-only SQLite, **schema v3**, **normalization v1**; manifest format 2.
- Exact included resources, coverage, record count, versions, hashes and sizes are
  declared by the manifest. The corpus verified on 2026-10-22 covers 2025-01 through
  2026-08: 20 sources and 102,405 occurrences. Future releases must declare their
  actual coverage; this is not a promise of historical completeness.
- Every source occurrence and provenance is preserved, including duplicates and
  lossless raw reconstruction. There is no ISBN/title deduplication or FTS5 requirement.

GitHub Release tag: `taiwan-new-books-index`.

The Android entry point is the [stable manifest](https://github.com/sapling1107/PageKeep-Data/releases/download/taiwan-new-books-index/taiwan-new-books-manifest.json),
available after the first successful publication. It names an immutable
`taiwan-new-books-<content_version>.sqlite.gz` asset in this same public release.
Manifest archives are named
`taiwan-new-books-manifest-<manifest_sha256>.json`.

The workflow builds and fully validates first, uploads the immutable gzip, downloads
it again to verify size/hash, archives manifests and verifies them, then updates
`taiwan-new-books-manifest.json` **last**. Previous valid immutable assets are retained
for rollback. Consumers must verify versions, sizes, hashes and SQLite before use.
Local validation does not publish; GitHub Actions uses only this repository's normal
`GITHUB_TOKEN`, with `contents: write`, to publish to itself. No PAT or cross-repository
authentication is required.

## Source data license and attribution

Source: 國家圖書館（National Central Library, Taiwan）— **臺灣出版新書預告書訊**.
Source data is used under the [Taiwan Government Data Open License, version 1.0
(OGDL-1.0)](https://data.gov.tw/license).

Retain the agency, dataset and required license/attribution notices when reusing or
redistributing the data and generated indexes. These notices are included in the
manifest and SQLite metadata. Do not imply official endorsement by the National
Central Library or Taiwan government. This source-data notice does not grant or
claim a separate software license for the pipeline code.
