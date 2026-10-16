# lakana-worker

> **Pre-beta.** Lakana is published as a 0.x pre-beta: the API and the data layout may change
> between releases, and it is not ready for production data yet. See the
> [project README](https://github.com/chrschy/lakana#readme).

The DuckDB query executor and dequeue loop. The only Lakana package allowed to
import `duckdb`. Never imports `lakana-server`; the queue table (`query_runs`,
in `lakana-core`) is the only channel between the two.

Part of [Lakana](https://github.com/chrschy/lakana). Installed with `pip install "lakana[server]"`.
