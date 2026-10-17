# Changelog

## [0.13.0](https://github.com/chrschy/lakana/compare/v0.12.1...v0.13.0) (2026-10-17)


### ⚠ BREAKING CHANGES

* **query:** Iceberg tables are read by field id. A renamed column keeps its data; a column dropped and added again, also by a type change with evolve_schema, is a new column whose older rows read as NULL. Reads of a table whose nested fields were renamed or re-added, that has a column type Lakana cannot read, or whose column names differ only in letter case are refused until the table is fixed. A commit through the Iceberg REST catalog that adds a data file without Iceberg field ids (for example pyiceberg's add_files) is refused (422), as is one of more than 2,000 data files or one whose check takes over 60 seconds; files committed earlier without field ids read as NULL, so write them again. A read under a row filter or column mask is refused when a column the policy uses was renamed or replaced since the policy was created; policies from earlier releases are judged against every schema the table has had. To repair, create each affected policy again for the column it is meant for, then revoke the old one; reads stay refused until the old one is revoked. Migration 0033 adds schema_id to row filters and column masks.

### Bug Fixes

* **policy:** replace the query engine's error text on reads under a row filter or column mask ([#122](https://github.com/chrschy/lakana/issues/122)) ([2a85e81](https://github.com/chrschy/lakana/commit/2a85e81330add357159ade5a20ad98f06a3dc4f0))
* **query:** read Iceberg tables by field id and refuse commits of files without field ids ([0fa4c38](https://github.com/chrschy/lakana/commit/0fa4c385c41fe867d2002ffee60b13f11fdf7af5))
* **query:** refuse a statement that changes data anywhere in a query ([#115](https://github.com/chrschy/lakana/issues/115)) ([47d946b](https://github.com/chrschy/lakana/commit/47d946bd83041d91e055c1bbbdcacd7bfeb323de))

## [0.12.1](https://github.com/chrschy/lakana/compare/v0.12.0...v0.12.1) (2026-10-16)


### Bug Fixes

* **query:** table-qualified columns work; policies on struct and promoted float columns ([#112](https://github.com/chrschy/lakana/issues/112)) ([938ac8c](https://github.com/chrschy/lakana/commit/938ac8c73660efe4e6eb937d56cbc5fcbbe30e82))


### Documentation

* the 0.12.0 upgrade row names all its migrations ([#113](https://github.com/chrschy/lakana/issues/113)) ([0cec6c5](https://github.com/chrschy/lakana/commit/0cec6c5c5e453a3732074b7def0e9a5c37b35a4e))


### Dependencies

* Bump mako from 1.4.1 to 1.4.2 ([#109](https://github.com/chrschy/lakana/issues/109)) ([026ee80](https://github.com/chrschy/lakana/commit/026ee806a28733db718d73289ae2711e75c4a35a))

## 0.12.0 (2026-10-16)


### Features

* first public release of Lakana ([b128830](https://github.com/chrschy/lakana/commit/b12883082763e9503d296ccf09aa8158cf6f7d0a))

## 0.11.0 and earlier

Earlier versions were developed before this repository was public and were not published to
PyPI. The changes from the first public release, 0.12.0, on are listed above.
