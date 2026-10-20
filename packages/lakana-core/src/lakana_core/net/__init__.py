"""Outbound HTTPS from Lakana's own processes, to addresses that someone else named.

- `lakana_core.net.egress` holds the checks every such client shares: the URL's shape, the
  refusal of internal addresses, the one name lookup with a deadline, connecting to the checked
  address (pinning), the TLS and proxy set-up, and the reduction of every failure to a fixed
  reason. The worker's webhook client (`lakana_worker.egress`) is built on it.
- `lakana_core.net.https_json` fetches one small JSON document (at most 64 KiB) from a public
  ``https`` address on port 443, without redirects, for the CI OIDC verification
  (`lakana_core.ci_oidc`).
"""
