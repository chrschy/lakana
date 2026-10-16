"""The run engine's database-side logic: selection, scheduling, leases, lockdown.

Everything here is DuckDB-free so the API server and the worker share it; the
worker adds execution (`lakana_worker.tasks`) and the scheduler thread
(`lakana_worker.scheduler`).
"""
