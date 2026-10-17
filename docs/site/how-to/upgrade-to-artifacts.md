# Upgrade to the release with artifacts

This is release 0.11.0, which adds [artifacts](../reference/artifacts.md): files such as trained models
that a Python asset stores and another asset reads. This page is for the person who runs the
instance. It says what the upgrade needs from you, in which order, and how to go back. What every upgrade asks, and what a release may break, is in the [Upgrade
policy](../explanation/upgrades.md).

## Does this need anything from me?

Everyone who upgrades needs two things, and everyone who has stored a secret needs a third (every
Compose bundle has: the storage secret of `dev`):

- **The database migrations `0018` and `0019`.** `0018` adds three tables and changes no existing
  row. `0019` adds two columns to the stored secrets (`format_version` and `key_id`) and marks
  every existing secret as format 1 under the key id `env`; it does not touch the encrypted bytes
  and needs no master key. The bundle runs both migrations for you when you start the new image
  (step 3 below); a local instance runs them every time it starts.
- **Every secret stored before this release set again.** This release does not read a secret in
  the old format by default, so until you set each one again, every deploy or query that needs it
  fails. It takes one command per secret, and no read of the old value; see
  [Secrets stored before this release](#secrets-stored-before-this-release).
- **The server and every worker on the new release, started together.** Do not leave a worker of
  the old release running next to the new server: it refuses the Python tasks of every project
  that is locked for the new release (see
  [Why every worker](#why-every-worker-and-then-the-projects)).

What else depends on your projects:

- **A project with only SQL assets needs nothing.** Its assets, tables, grants and runs keep
  working as before.
- **A project with Python assets must be locked again for the new release.** Before version 1.0,
  the `lakana` in a project's `uv.lock` must have the same minor version as the instance. After the
  upgrade, the lock of every project with a Python asset names the old release, so until you lock
  and deploy it again, each of its Python tasks fails with this message (your two versions differ):

  <!-- output -->
  ```text
  uv.lock: has lakana 0.9.4, but this instance runs 0.10.0 (before 1.0 the minor versions must match); update the dependency and run `uv lock`. This project version was deployed for another platform version: update the lock and redeploy.
  ```

  The same happens after every minor release, not only this one. Its SQL assets still run. Step 5
  below says what to do; do it right after step 4.
- **Nothing else changes.** An asset without `output` is a table asset, as before. The new settings
  below all have defaults.

You do need more if you run your own storage backend (see
[A custom storage backend](#a-custom-storage-backend)), if you use an object store (see
[Interrupted uploads](#interrupted-uploads-on-an-object-store)), or if you want to go back (see
[Going back](#going-back)). Read [Going back](#going-back) before you start: the way back loses
data once someone has stored an artifact.

## The order

1. **Back up first.** Take the Postgres dump and the storage backup of
   [Backups](deploy-with-compose.md#backups). A backup taken now is the simplest way back. For a
   local instance, stop it (Ctrl-C in its terminal) and copy its data directory, by default
   `~/.local/share/lakana/local`: that copy is the only way back for a local instance (see
   [Going back](#going-back)).
2. **Pull the new image, and compare `compose.yaml`.** Follow the two checks and the commands of
   [Upgrading](deploy-with-compose.md#upgrading), then set `LAKANA_IMAGE` in `.env` to the new
   tag.
3. **Start the new release.** Run the last command of
   [Upgrading](deploy-with-compose.md#upgrading): it pulls the image and starts the stack with
   `up -d --wait`. The `migrate` service runs `0018` and `0019` first, and the server and the workers start
   after it has finished, all on the new image. When it worked, the command returns without an
   error. If `migrate` fails, `up --wait` stops with an error and the server and the workers do
   not start on the new image; read `docker compose logs migrate`. If you scaled the workers by
   hand or run some outside the bundle, upgrade each of them to the same release now. The bundle
   sets its own storage secret again in this step; set every secret you created yourself again, as
   in [Secrets stored before this release](#secrets-stored-before-this-release), before anything
   needs them.
4. **Check the workers.** `docker compose logs worker` shows every worker's start-up. A worker of
   this release prints its INFO lines (the time, the level, the logger name and the message), so
   you will see more output than before, among it `Python assets: enabled` or `Python assets:
   disabled` with the reason. Nothing in it is a fault.
5. **Lock every project with Python assets again, and deploy it.** Do this only when every worker
   is on the new release. For each project that has a `.py` file under `assets/`:
   1. In `pyproject.toml`, change the pin of `lakana[assets]` to the new release. You get the
      number from `lakana --version`, which prints the instance's version too. Where `lakana`
      comes from (the public package index, or a folder `wheelhouse` of your project for a build
      that is not published) is unchanged; if it is the folder, put the new wheel there and remove
      the old one (see "Python assets", step 4, in [Projects and deploys](projects.md)).
   2. Run the lock:

      <!-- source: docs/examples/train-and-predict/run.sh#lock -->
      ```bash
      uv lock
      ```

   3. Commit `pyproject.toml` and `uv.lock`, and deploy the project again, as in
      [Commit, deploy and run](../tutorials/train-a-model.md#commit-deploy-and-run).

   When it worked, the deploy ends with `Deployed <repository> in <environment> at <commit>`
   instead of an error that names `uv.lock`, and the project's Python tasks run again. Deploy each environment that runs the project. A project with only SQL assets has
   nothing to do here. A deploy of this release writes manifest version 3 for every project.
6. **Then deploy a project that has an artifact asset.** An artifact asset needs a worker of this
   release (see below), and step 5 has already made sure of it.

A local instance (`lakana server start --local`) needs only the upgrade of the `lakana` package:
it runs the migration when it starts, and the server and the worker are the same installation.
Step 5 applies to it as well.

### Why every worker, and then the projects

A worker of the previous release does not know the new release's lock. Every Python task starts
with a check of the project's `uv.lock` against the worker's own version, and a worker of the
previous release refuses a lock that names the new release, for good, with the message above
(its two versions the other way round: the lock names the new release, the worker runs the old
one). That holds for **every** Python task of such a project, table assets and artifact assets
alike. Its advice, "update the lock and redeploy", is
the wrong action on that worker: the right one is to **upgrade the worker**, not to lock again. SQL
assets do not run this check, so they run on a worker of the old release too.

The order of step 5 follows from that: lock and deploy a project only when every worker is on the
new release, so that none of them can pick up its tasks and refuse them.

A worker of this release does the opposite check for the manifest: it refuses a manifest of a
version newer than it knows, for good, with `the project version's manifest has version N, but this
worker understands versions 1 to M; update the worker to the platform version that deployed it, or
redeploy with this worker's version` (N is the newer number; the worker of the artifacts release,
0.11.0, understands versions 1 to 3, and a later worker understands more). Upgrade the workers,
then start the run again.

## Secrets stored before this release

This release ties every secret to its own row (see [Manage secrets](manage-secrets.md)) and no
longer reads a secret stored by an earlier release unless you say so. **After the upgrade, the
first deploy or query that needs such a secret fails** with a message like this one (the names
differ):

<!-- output -->
```text
Secret 'seaweedfs-s3' in environment 'dev' is stored in the old format (1), which Lakana does not read by default. Set it again with `lakana secret rotate seaweedfs-s3 --env dev --values-stdin`; that needs no read of the old value. To keep old secrets working until you have, start the server and the workers with LAKANA_SECRETS__ACCEPT_FORMAT_1=true, and remove the setting afterwards.
```

In a deploy the message is the run's error; in a query or a table request it is the answer of the
server. Every Compose bundle has one secret that Lakana created itself, `seaweedfs-s3` in `dev`: the
keys of its storage. **The bundle sets that one again for you:** its `setup` service runs at every
start, finds it at `format_version` 1, and sets it again with the keys from `.env`; its log says
`Set storage secret 'seaweedfs-s3' again in the current stored format`. You do nothing for it, and
no query or deploy needs it before `setup` has finished. A `dev` that you bound to a storage secret of
another name is left alone, like everything else `setup` does not own. If you had put keys of
your own under the name `seaweedfs-s3` with the earlier release, `setup` replaces them with the keys
from `.env`. For the secrets **you**
created, there are two ways forward, in the order you need them:

1. **Set the secrets again, before anything needs them.** List what you have with
   the list command of [Manage secrets](manage-secrets.md) (every secret whose `format_version` is
   `1`), and set each one again with the rotate command shown in the message above, giving all the
   values you stored. You need those values from wherever you keep them: Lakana does not show a
   stored secret. When it worked, the list shows `format_version` `2` for it. Nothing else needs
   to change, and you never need the setting.
2. **If you cannot do that before the first deploy or query, accept old secrets for a while.** Set
   `LAKANA_SECRETS__ACCEPT_FORMAT_1=true` on the server and on every worker (in the bundle, add it
   to `x-lakana-env`: [Secrets settings](deploy-with-compose.md#secrets-settings)), start them,
   set every secret again as in step 1, then remove the setting and start them again. While it is on,
   the server and each worker log a `WARNING` at start-up, and any old-format ciphertext written
   under your master key is accepted on any row.

Lakana does not set the secrets you created again for you: it could not tell an old secret from an
old-format ciphertext that someone pasted onto a row, and the migration has no master key. The
bundle's own storage secret is the exception, because the bundle knows its values. If any of these
credentials mattered, also rotate it at its provider (a new access key, a new token) after you set
it again here: that makes every copy of the old ciphertext worthless.

## What is new, and what you can set

Four settings, all optional. They bound what one artifact version may be and how long an upload may
stay open; the defaults and their meaning are in
[Storage for artifacts](deploy-with-compose.md#settings):

| Variable | Default |
|---|---|
| `LAKANA_ARTIFACTS__MAX_FILE_BYTES` | 2 GiB |
| `LAKANA_ARTIFACTS__MAX_BYTES` | 10 GiB |
| `LAKANA_ARTIFACTS__MAX_FILES` | 10000 |
| `LAKANA_ARTIFACTS__UPLOAD_TTL_SECONDS` | 3600 |

One setting you may already have changed now does more. `LAKANA_RUN__SWEEP_INTERVAL` (default
`3600` seconds) paces the worker's clean-up of attempt directories, and since this release also
the artifact store's sweep: expired uploads, files no version needs and manifests of deleted
versions. Only the worker that holds the scheduler role runs the artifact sweep. If you set the
interval to `0`, nothing collects those leftovers; see
[The sweep](deploy-with-compose.md#the-sweep).

Artifacts live under `artifacts/` in each environment's storage root, and Lakana now refuses to
put a table there. Lakana itself never did: it places managed tables under `managed/` and external
tables under `raw/`. Check once, before step 3, that nothing of yours is in `artifacts/` of any
environment's storage root: list the root with your store's own tools (or `ls` for a `file://`
root). If there is no `artifacts/` directory, you are done. If there is one with files of yours,
move them out before step 3, because the artifact sweep treats a directory there as an artifact's.
If one of them belongs to a table (a table whose `metadata_location`, shown by `lakana table
describe`, lies under `artifacts/`), a write to that table is refused from the upgrade on with
`Location '…' is inside the directory artifacts/ of the environment's storage root, which is
reserved for artifacts. Choose a location outside it.` Copy its rows into a table that Lakana
creates, then drop the old one. See
[Storage for artifacts](deploy-with-compose.md#storage-for-artifacts).

## A custom storage backend

Only if you wrote your own `Storage` implementation (the built-in `file://` and S3-compatible
backends are done). The storage interface gained two methods, and a backend without them fails
when an artifact is stored or read:

- `open_write(path)` returns a `StreamWriter` with `write`, `commit` and `abort`; nothing is
  visible at `path` until `commit`.
- `read_chunks(path, chunk_bytes=...)` yields an object's bytes in pieces without loading it.

The contract of both is in the docstrings of `lakana_core.storage.base.Storage`.
## Interrupted uploads on an object store

If you use an S3-compatible store, set the bucket rule that aborts incomplete multipart uploads. A
server that dies in the middle of a file leaves parts that Lakana cannot find. The rule, an
example, and what to do when your store does not support it are in
[Interrupted uploads on an object store](deploy-with-compose.md#interrupted-uploads-on-an-object-store).
It matters from the day the first artifact is stored, not before.

## Going back

**Going back after artifacts were stored loses them, and loses the secrets you set after the
upgrade.** Downgrading migration `0018` deletes the catalog record of every artifact and of every
version, and the files stay in storage where nothing points at them. Downgrading `0019` deletes
every secret stored at format 2, that is every secret created or set again since the upgrade,
because the previous release cannot decrypt one; the secrets still at format 1 are left as they
were (the previous release reads them without any setting). There is no command in `lakana` or `lakana-server` that downgrades; the one below
calls the migration directly. The previous image cannot start on a database that is at `0018` or `0019`,
so the downgrade (step 3) is always needed. If no project has an artifact asset and nobody has
stored an artifact, nothing is lost, and you can skip steps 1 and 2.

**A local instance** (`lakana server start --local`) has no step 3. Its start migrates the
database up, never down, and the previous `lakana` package cannot find revision `0019`, so it
stops at start. The only way back is to stop the instance and put back the copy of its data
directory that you made in step 1 of [The order](#the-order); everything changed since then is
lost. The rest of this section is for the Compose bundle.

What the downgrade removes:

- every secret with `format_version` 2 (`lakana secret list` shows it), and the two columns
  `format_version` and `key_id`: list those secrets before you go back and keep their values. The
  rows are gone, so on the previous release you create them again with `lakana secret create`
  (`rotate` finds nothing). An environment or a repository that references a deleted secret fails
  with `does not exist or has been revoked` until you have;
- the tables `artifact_versions`, `artifact_uploads` and `artifact_blobs`, with every row;
- every catalog entry of an artifact (the rows of `tables` whose type is `ARTIFACT`), because the
  previous release cannot load a table of that type.

What it leaves:

- **The files** under `artifacts/` in each environment's storage. After the downgrade the catalog
  no longer knows which directory belongs to which artifact, so remove them before (step 2) or
  with your store's own tools.
- **The audit rows** of the artifact events (`ARTIFACT_*`): audit rows are never deleted. The
  audit listing of the previous release cannot load them, so it may fail until you upgrade again.
- **Grants, row filters and column masks** that named a deleted artifact stay as rows that match
  nothing.
- **Lineage edges** that name a deleted artifact stay too.

In this order:

1. **Decide what you keep.** If you want a model back later, download its versions now with
   [Inspect and download artifact versions](inspect-and-download-artifact-versions.md). After step
   3 the files cannot be tied to a name any more.
2. **Remove the artifact assets, and then the artifacts, still on the new image.** Change every
   project that has an artifact asset so that it has no `output="artifact"` and no artifact input,
   and deploy it again. Do not change the lock yet: the new instance refuses a lock that names the
   previous release (the message of [the first section](#does-this-need-anything-from-me)), so the
   lock keeps naming the new one until step 5. (A project with an artifact asset was deployed on
   the new release, so its lock names it already.) Then remove each artifact and its files with
   [Delete an artifact version](delete-an-artifact-version.md#drop-the-whole-artifact), in each
   environment. Dropping is what removes the files from storage.
3. **Stop everything and downgrade, still on the new image.** The migrations `0018` and `0019` exist
   only in the new release. The revision to go back to is `c8e1a5d7f3b9`, the one before `0018`;
   the command undoes both:

   <!-- not tested: changes a real database -->
   ```bash
   docker compose stop server worker
   docker compose run --rm --no-deps migrate python -c "from lakana_core import migrate; from lakana_core.config import Settings; migrate.downgrade(Settings().database.url, 'c8e1a5d7f3b9')"
   ```

   When it worked, the command ends without a traceback. Do not start the old image before it
   has: the previous release fails wherever it loads an artifact's catalog row, and its `migrate`
   service cannot find revision `0019`, so the server and the workers would wait for it for good.
   If the command fails, do not start the old image either: start the new image again with the
   last command of [Upgrading](deploy-with-compose.md#upgrading), read the error, or restore the
   backup of step 1 of [The order](#the-order) with
   [After a restore](deploy-with-compose.md#after-a-restore), on the previous image.
4. **Put the old image back.** Set `LAKANA_IMAGE` in `.env` to the previous tag, restore any
   `compose.yaml` change you made for this release, and run the last command of
   [Upgrading](deploy-with-compose.md#upgrading) again. The old `migrate` finds the database
   already at its revision and changes nothing. From now on, every Python task of a project whose
   lock names the new release fails with the lock message, until step 5.
5. **Lock every project that you locked for the new release for the previous release again.** In
   `pyproject.toml`, change the pin of `lakana[assets]` back to the previous release (if `lakana` comes from the
   folder `wheelhouse`, put the previous wheel there and remove the new one), run `uv lock`, commit and deploy again.
   SQL assets are not affected and need nothing.

The other way back is to restore the backup of step 1 of [The order](#the-order) with
[After a restore](deploy-with-compose.md#after-a-restore), on the previous image. It returns the
catalog and the storage to that moment and loses everything changed since: runs, deploys and
artifacts alike. The deploys in the restored catalog are the old ones, with their old locks, so
they run. A project whose repository already holds the new lock needs step 5 before its next
deploy.
