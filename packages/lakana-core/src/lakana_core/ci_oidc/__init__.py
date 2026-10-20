"""Verification of the OIDC ID tokens of CI providers (GitHub Actions, GitLab CI).

- `config` holds the operator settings (`LAKANA_CI_OIDC__*`).
- `jwks` finds and caches each issuer's signing keys.
- `verification` checks one ID token, in a fixed order, and names every refusal with a stable
  code.

The exchange of a verified token for a Lakana credential, and the single use of its `jti`, are
not here: they need the database and live with the endpoint.
"""
