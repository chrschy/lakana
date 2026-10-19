# Changelog

## [0.16.0](https://github.com/lakana-labs/lakana/compare/v0.15.0...v0.16.0) (2026-10-19)


### ⚠ BREAKING CHANGES

* **server:** `GET /api/v1/grants` without a filter now returns every grant, paged (100 per page; follow `X-Next-Cursor`), where it returned only the instance grants. To list the instance grants, filter the result to grants without an environment. The principal and group lists now come newest first, 100 per page, and are paged the same way; pass `limit` and follow `X-Next-Cursor` to read them all.

### Features

* **catalog:** describe tables and columns ([#183](https://github.com/lakana-labs/lakana/issues/183)) ([daeb547](https://github.com/lakana-labs/lakana/commit/daeb547dd4a91827cd971df53fd2c4eee14707ba)), closes [#140](https://github.com/lakana-labs/lakana/issues/140)
* **catalog:** show a table's health ([#184](https://github.com/lakana-labs/lakana/issues/184)) ([d0cd6eb](https://github.com/lakana-labs/lakana/commit/d0cd6eb50961b105acb08b8198351844a68f1586)), closes [#141](https://github.com/lakana-labs/lakana/issues/141)
* **core:** stable failure codes with a next step for tasks ([#188](https://github.com/lakana-labs/lakana/issues/188)) ([9f7068c](https://github.com/lakana-labs/lakana/commit/9f7068cc4cc31d5a0028c2e95d558c51914322a4)), closes [#145](https://github.com/lakana-labs/lakana/issues/145)
* **server:** filter and page the runs, queries and deploys lists ([#186](https://github.com/lakana-labs/lakana/issues/186)) ([c29a457](https://github.com/lakana-labs/lakana/commit/c29a4572e9062deb5ede196bb36aefe7c1e6c530)), closes [#143](https://github.com/lakana-labs/lakana/issues/143)
* **server:** list grants, explain permissions, search principals and groups ([#191](https://github.com/lakana-labs/lakana/issues/191)) ([225df7c](https://github.com/lakana-labs/lakana/commit/225df7cb6405f601995265ab5b28fc6092de8615)), closes [#147](https://github.com/lakana-labs/lakana/issues/147)
* **server:** read a task's log from an offset ([#187](https://github.com/lakana-labs/lakana/issues/187)) ([f117cd6](https://github.com/lakana-labs/lakana/commit/f117cd67e4e693d5f0175f9f13f39ffa91ed44cd)), closes [#144](https://github.com/lakana-labs/lakana/issues/144)
* **server:** search tables, columns, runs and people ([#185](https://github.com/lakana-labs/lakana/issues/185)) ([217df80](https://github.com/lakana-labs/lakana/commit/217df80f662aaea8b51ace8a2ac570130199d400)), closes [#139](https://github.com/lakana-labs/lakana/issues/139)
* **server:** show a job's triggers ([#190](https://github.com/lakana-labs/lakana/issues/190)) ([3c222b2](https://github.com/lakana-labs/lakana/commit/3c222b2a4140070bd921cec85d73830b66dc9b94)), closes [#146](https://github.com/lakana-labs/lakana/issues/146)


### Bug Fixes

* keep file names in long paths, bound commit-check reads, name a lock wait, faster CLI tests ([#193](https://github.com/lakana-labs/lakana/issues/193)) ([f99dd50](https://github.com/lakana-labs/lakana/commit/f99dd50b4c475cc8dd89cf9fee1106f50d463df8)), closes [#165](https://github.com/lakana-labs/lakana/issues/165) [#171](https://github.com/lakana-labs/lakana/issues/171) [#95](https://github.com/lakana-labs/lakana/issues/95) [#101](https://github.com/lakana-labs/lakana/issues/101)
* **policy:** check a row filter's or column mask's types at creation, and keep a mask's column type ([#177](https://github.com/lakana-labs/lakana/issues/177)) ([42710ba](https://github.com/lakana-labs/lakana/commit/42710ba76403fadce4a5dbedfa516612ba15c5fa)), closes [#126](https://github.com/lakana-labs/lakana/issues/126)
* **worker:** create the attempts directory at start, not in the image ([#181](https://github.com/lakana-labs/lakana/issues/181)) ([ba6c97a](https://github.com/lakana-labs/lakana/commit/ba6c97a619104ad3d1be503ddc4bd09b1a556874)), closes [#173](https://github.com/lakana-labs/lakana/issues/173)

## [0.15.0](https://github.com/lakana-labs/lakana/compare/v0.14.0...v0.15.0) (2026-10-18)


### ⚠ BREAKING CHANGES

* **security:** while an environment has a storage secret, changing its storage options (`lakana env set-storage --option`) needs MANAGE_SECRET in that environment as well as MANAGE_ENVIRONMENT; before, MANAGE_ENVIRONMENT was enough and the secret could be sent to another endpoint. Someone without MANAGE_SECRET is now refused (403, audited); an administrator makes the change. Migration 0035 adds a CHECK constraint on a secret's use and stops if an existing row is inconsistent, naming it.

### Bug Fixes

* clean up task directories on macOS, gate releases on one approval, and check for copied code ([#167](https://github.com/lakana-labs/lakana/issues/167)) ([b214b9f](https://github.com/lakana-labs/lakana/commit/b214b9fbefdfb8d2d7f6065b9c17a4329db6f580)), closes [#164](https://github.com/lakana-labs/lakana/issues/164) [#155](https://github.com/lakana-labs/lakana/issues/155) [#153](https://github.com/lakana-labs/lakana/issues/153) [#157](https://github.com/lakana-labs/lakana/issues/157)
* **security:** storage options need the right to manage secrets; a memory stop records each task ([1378456](https://github.com/lakana-labs/lakana/commit/13784569215cad10427f6550bdbe38ce4077582c))


### Documentation

* the upgrade notes name 0.15.0 ([#174](https://github.com/lakana-labs/lakana/issues/174)) ([ad32fab](https://github.com/lakana-labs/lakana/commit/ad32fab37207712eaf24fad0eca2d8152bdffd43))


### Dependencies

* bump hypothesis from 6.169.3 to 6.169.4 in the python group ([#169](https://github.com/lakana-labs/lakana/issues/169)) ([e069235](https://github.com/lakana-labs/lakana/commit/e069235d907901e26a142886fefa13e97b9b9597))
* bump ty from 0.0.87 to 0.0.88 ([#170](https://github.com/lakana-labs/lakana/issues/170)) ([2374e9b](https://github.com/lakana-labs/lakana/commit/2374e9b97403bd1f2f701e550108f9c9938b8e28))

## [0.14.0](https://github.com/lakana-labs/lakana/compare/v0.13.0...v0.14.0) (2026-10-18)


### ⚠ BREAKING CHANGES

* new images are published as ghcr.io/lakana-labs/lakana instead of ghcr.io/chrschy/lakana. A Compose bundle written by an earlier `lakana bundle init` names the old image in its .env; change LAKANA_IMAGE to ghcr.io/lakana-labs/lakana:<new version> before you pull. Old links to github.com/chrschy/lakana redirect.
* **secrets:** A secret now has one use (a package index, a repository, or its own environment) and Lakana sends it nowhere else. Migration 0034 binds a secret only when exactly one index or repository names it and its environment does not use it for storage or a webhook; every other secret stays unbound, keeps serving its environment's storage and webhooks, and is refused to every index and repository until it is bound. A secret created without a use is unbound as well. Only someone who may manage secrets (ADMIN) binds: holders of MANAGE_PACKAGE_INDEX or MANAGE_REPOSITORY alone can name only a secret already bound to that entry, and changing the URL of an entry with a bound secret needs MANAGE_SECRET in that secret's environment. After upgrading, an administrator runs `lakana secret list --env ENV` in each environment, checks the URLs in `lakana index list` and `lakana repo list`, and binds every credential that shows no use: `lakana secret bind NAME --env ENV --index INDEX` (or `--repository REPO`), and storage and webhook secrets with `--own-env`. A secret serves one entry: where several indexes or repositories shared one, create one secret per entry (`lakana secret create NEW --env ENV --repository REPO --values-stdin`) and point the entry at it (`lakana repo update REPO --credential-secret NEW`). Scripts that create a secret for an index or repository add `--index NAME` or `--repository NAME` to `lakana secret create`.

### Features

* clearer access commands, lakana server stop, a dependency canary, and docs on Pages ([7b6fe89](https://github.com/lakana-labs/lakana/commit/7b6fe892f61fd42a18c0b226c514ee3ca8bd440e))
* **cli:** a sample project in lakana init, and lakana server reset --local ([#133](https://github.com/lakana-labs/lakana/issues/133)) ([6d21c7f](https://github.com/lakana-labs/lakana/commit/6d21c7f547abd7bb5c0366c2dfb7cbb1da77e717))
* **secrets:** bind each secret to one use, and refuse it for any other ([bb3f477](https://github.com/lakana-labs/lakana/commit/bb3f4774790640f2f0fcf196c6fd161eb709acfb))
* **worker:** add the task launcher, the only holder of the Docker socket ([#151](https://github.com/lakana-labs/lakana/issues/151)) ([ee55bc9](https://github.com/lakana-labs/lakana/commit/ee55bc9edf5a0e253ae0628f49dfcf8a11ea653e))


### Bug Fixes

* **security:** keep SeaweedFS's internal APIs off the network the worker uses ([#132](https://github.com/lakana-labs/lakana/issues/132)) ([715647e](https://github.com/lakana-labs/lakana/commit/715647e86d14b2e101772cf90068dd89514fb303))
* **worker:** replace a connection the server reset, and give maintenance a time limit ([#137](https://github.com/lakana-labs/lakana/issues/137)) ([497f41c](https://github.com/lakana-labs/lakana/commit/497f41ceb57928a8fb7d60fbc9a08afa77eea266))
* **worker:** treat a process group of exited processes as gone on macOS ([#152](https://github.com/lakana-labs/lakana/issues/152)) ([a710add](https://github.com/lakana-labs/lakana/commit/a710add782e60e3b088bd07cf01874091b22dc90))


### Documentation

* the upgrade notes name 0.13.0 and 0.12.1 ([#124](https://github.com/lakana-labs/lakana/issues/124)) ([9e7ad74](https://github.com/lakana-labs/lakana/commit/9e7ad74e2d5a980cfe53ef8cb578046d5da62343))
* the upgrade notes name 0.14.0 ([#163](https://github.com/lakana-labs/lakana/issues/163)) ([561716b](https://github.com/lakana-labs/lakana/commit/561716b853ba6c95d17bfc6bb7a129c62d9f0ce9))


### Dependencies

* bump alpine from 3.22 to 3.24 in /packages/lakana-worker/tests/sshd in the test-images group ([#158](https://github.com/lakana-labs/lakana/issues/158)) ([642303f](https://github.com/lakana-labs/lakana/commit/642303f3959162e2435a15dad08c1d59b2bb225d))
* bump the python group with 2 updates ([#159](https://github.com/lakana-labs/lakana/issues/159)) ([6d09852](https://github.com/lakana-labs/lakana/commit/6d09852de0a595b5ab7ec4b593b31b61216a66f3))
* bump ty from 0.0.86 to 0.0.87 ([#160](https://github.com/lakana-labs/lakana/issues/160)) ([f926e83](https://github.com/lakana-labs/lakana/commit/f926e837412d46fbb3cb5a63b464f0c4b61e1977))

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
