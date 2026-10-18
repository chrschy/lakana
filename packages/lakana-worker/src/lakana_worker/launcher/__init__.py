"""The launcher: the one process of a stack that holds the Docker socket.

It starts every Python task and every environment build in a container of its own, from one
narrow request the worker sends over a Unix socket. Whoever can talk to the Docker socket is
root on the host, so this package is kept small, uses only the standard library, imports nothing
else of Lakana (an import-linter contract and `test_launcher_imports.py` hold it to that) and
builds every Docker Engine API call itself. It runs as its own Compose service (`lakana-launcher`)
with no network; the egress proxy of the builds (`lakana-egress`) lives here for the same reasons.
"""
