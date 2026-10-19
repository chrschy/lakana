# Python assets: what they are for and how far they are trusted

Why a project has Python assets next to its SQL, and why one instance is one trust domain. The
steps are in [Projects and deploys](../how-to/projects.md#python-assets); the rules are in
[Python assets (reference)](../reference/python-assets.md).

**SQL transforms, Python at the edges.** Joins, filters and aggregations belong in SQL assets.
The platform reads their dependencies and column lineage, runs them on DuckDB and enforces
limits and policies there. A Python asset is for what SQL cannot do at either edge of a
pipeline: ingestion (no inputs, returns rows from an API or a file format SQL cannot read) and
end use (scoring with a model, calling a library, training). If you can write it as a `SELECT`,
write it as a SQL asset.


## Isolation: one instance is one trust domain

**`LocalProcessRunner`, which runs Python assets, is not a sandbox.** A task runs as the
worker's operating-system user. It can read everything the worker can read: the worker's own
settings and credentials (its database and storage access among them), the files in its home
directory, and anything it can reach on the network. The venv cache is shared between tasks and
writable by them, so one task can change the environment another task will use. The scrubbed
environment, the private temporary directory and the task token limit accidents; they do not
stop code that means harm.

So one instance is one **trust domain**: everyone who may deploy Python code to it is trusted
with everything the worker can read. Deploy only code and dependencies you would run on the
worker itself, and run one instance per client or per group of people who do not trust each
other. Task isolation comes with `DockerRunner` (#38), which is planned before the public
beta; until then, treat a Python asset like code you run on the worker's machine
yourself.
