# Artifacts: what they are and what they are not

After reading this page you can decide who may read and write your artifacts, which formats are safe
to load, and what deleting a version does and does not remove. It explains choices. The facts, the
commands and the limits are in the [reference](../reference/artifacts.md). The steps to use
scikit-learn are in [Train a scikit-learn model and score with it](../how-to/models-with-scikit-learn.md).

## What an artifact is

An **artifact** is a file, or a directory of files, that a Python asset stores and another asset
reads. The usual example is a trained model. Lakana stores the bytes exactly as they are. It never
looks inside them, and it does not turn Python objects into files: your code decides the format
(`joblib`, JSON, a framework's own files), writes it into `lakana.output_dir()` and returns the
path.

An artifact has a name in the same catalog as tables: `models.churn_model` is
`<namespace>.<name>`. One name means one thing. A name is a table or an artifact, never both, and
a deploy refuses a clash.

A model is not rows, so it does not belong in a table. A table stays what SQL can query. An
artifact is for what only your code can open.

## What a version means

Each run that stores an artifact makes a new **version**, numbered from 1. A version is
immutable: Lakana never changes its files. A version that a run stores is made by one task attempt
of one run of one git commit, and Lakana records which, so you can go from a model back to the code
that produced it. A version that a person uploads has no run and no commit.


A version is identified by its **manifest**, the list of its files with their paths, sizes and
SHA-256 hashes. The digest of the manifest (`manifest_sha256`) is the identity of the content: the
same files give the same digest, whichever run stored them.

A consumer (an asset that reads the artifact; the asset that stores it is the producer) reads one
version, chosen like this: the newest version that a task of **its own run**
stored, and otherwise the **current version**, the highest one whose files still exist. In a
run where the producer comes first, the consumer reads what that producer just made. A run that
reads only (the producer is not part of it) reads the current version, whoever stored it. The
version a task read is recorded in the lineage of what it wrote.

There are no named pointers yet, such as `production`, and no way to pin a version. The current
version is the only pointer.

## Who can produce an artifact you read

Anyone who can store a version of an artifact decides what your consumer loads. Two kinds of
principal can:

- **Whoever can deploy code to the repository that produces it.** The producing asset runs
  with that code. This is the same trust you already place in a repository: see "Python assets" in
  [Projects and deploys](../how-to/projects.md).
- **Whoever holds `WRITE_TABLE` on the artifact's name**, for example through a grant on its
  namespace. That includes the role `DATA_SCIENTIST`. Such a person can run
  `lakana artifact upload` and add a version outside the pipeline. A consumer that is not part of
  a run of the producer then reads that version as the current one.

In a protected environment only a run of the deployed code can add a version. A person needs
`MANAGE_ENVIRONMENT` and a written reason, and the write is audited.

So decide who gets `WRITE_TABLE` on a namespace that holds models with the same care as who may
deploy. See "A pickled model is code" for what a consumer does with a file that someone else wrote.

## Access is all or nothing, and a model reveals what it was trained on

**A person who may read a model can learn about rows they are not allowed to query.** Granting
read on an artifact is granting a view of its training data.

Lakana can hide rows and mask columns of a table. It cannot do that for an artifact, because it
does not know what is inside a file. Row filters and column masks therefore cannot be attached to
an artifact. Whoever may read an artifact reads all of it.

This has three consequences:

- A model trained on a table that has a row filter or a column mask is readable by **everyone who
  has read on its namespace**, filter or no filter. The training read applied the policies of the
  run principal. The model keeps what it learned.
- A **grant on a namespace covers its tables and its artifacts**. Lakana does not tell a table
  from a model in a grant. Someone with read on `gold` reads every model that lives in `gold`.
- Grant read on a model as carefully as on its training table. Keep models in a namespace of their
  own, such as `models`, and grant that namespace on purpose. Give read on a single artifact when
  one person needs only that one.

## A pickled model is code

Many libraries save a model with `pickle`, and `joblib` does the same. **Loading such a file runs
whatever the file contains.** A file that someone built on purpose can run any Python code while
it is loaded. Reading a pickle is running a program.

In an asset, that code runs inside the consuming task:

- It has the rights of the task's token: it can read the task's declared inputs and write the
  task's own output.
- It runs as the worker's operating-system user, and the local runner is not a sandbox. What the
  worker can read, such as its settings and credentials, the code can read too. One Lakana instance
  is one trust domain: see [Isolation](python-assets.md#isolation-one-instance-is-one-trust-domain).

So declare only artifacts as inputs whose producers you trust, and think about who holds
`WRITE_TABLE` on them (see above). The same holds when you load a downloaded copy on your own
laptop.

Formats that hold numbers and structure, not code, such as JSON, safetensors or ONNX, are
designed not to run code when their own readers open them. Prefer one of them when your library
offers it. A bug in a reader is a separate matter, which Lakana cannot prevent.

## How it is stored

Files are stored by their content hash, per artifact:
`artifacts/<artifact id>/blobs/<sha256>` under the environment's storage root, and one manifest
per version in `artifacts/<artifact id>/manifests/<version>.json`. The artifact's id, not its name,
is in the path, so a renamed or recreated name never reaches old bytes.

- A file that did not change between two versions is stored once and **not sent again**: a new
  version that changes one file sends one file.
- **Nothing is shared between artifacts.** If two artifacts hold the same file, each stores its own
  copy. Sharing would tell a writer whether another artifact contains a given file, and would make
  deleting depend on objects the person deleting may not see.
- The bytes pass through the Lakana server. The server never holds a whole file in memory, but
  its network carries each file. There is no direct transfer to object storage yet.
- Every download is checked against the manifest by the reader. A file that does not match is
  refused and nothing is kept.

## What deleting means

Deleting a version removes its **bytes**, except a file that another live version of the same
artifact still has. Lakana has no undo. If your bucket keeps old object versions, a deleted file
stays in the bucket until the bucket's own rule removes it.

What stays after a deletion:

- the **version's record**: its number, size, who made it, which run and commit, and who deleted
  it and when. `artifact versions list` still shows it, marked deleted. The number is never used
  again.
- the **names and hashes** of its files, kept in the catalog with the record. Their contents are
  gone.
- the **lineage** that names the version.
- **copies** that people already downloaded, and the contents of any backup you made. Lakana does
  not reach them.

Who may delete: a person or service account with `DROP_TABLE` on the artifact, never a task
token. **A run can also delete.** An asset with `keep_versions=N` removes the older versions when
a new one is stored, without anyone holding `DROP_TABLE`. Whoever may deploy that asset
therefore controls how much of its history stays, and a deleted version cannot be brought back.

Bytes go at once, or shortly after: a request removes up to about 200 files or five seconds' worth
and leaves the rest to a periodic **sweep**. Dropping an artifact removes every version, its
files and its grants. A new artifact of the same name starts again at version 1.

The sweep runs in the worker that holds the scheduler role, every `LAKANA_RUN__SWEEP_INTERVAL`
seconds (3600 by default). It removes what a crash or an abandoned upload left: uploads that
expired, files that no live version and no open upload needs, half-written files, and the
manifests of deleted versions. A restored database or a failover can forget things that still
exist, so the sweep keeps what such a loss would leave: a directory without a catalog row (unless a
drop marked it), and a manifest of a version the catalog does not know, together with the files
that manifest names. A file with no record in the catalog is removed only after seven days, or at
once when the manifest of a deleted version names it, and never while a manifest the catalog does
not know, a version that is not deleted or an upload in progress names it. After a restore, read "After a restore" in
[Deploying with Compose](../how-to/deploy-with-compose.md).

Deleting an **environment** removes the artifacts' catalog rows and leaves their files in
storage, as it leaves the data of tables.

## What is not there yet

- **Named pointers and pinned versions.** There is no `production` alias, and an asset cannot
  name the version it reads: it gets its own run's version or the current one. A person can
  still download any live version with `--version`. Named pointers arrive with the model
  registry, in a later release.
- **Experiment tracking.** Parameters and metrics of a run are not attached to a version. A
  later release builds that on the artifact store.
- **Direct transfer to object storage.** Files go through the server.
- **A download cache.** Each task downloads its inputs again.
- **SQL assets that read an artifact,** an asset with both a table and an artifact output, and an
  artifact that reads its own earlier version.
- **Artifacts in the BI endpoint and in the Iceberg REST catalog.**

The [roadmap](roadmap.md) plans the first two (named pointers and experiment tracking, with the model registry). The
others are not scheduled.
