# Security policy

## Reporting a vulnerability

The primary channel is GitHub's [private vulnerability
reporting](https://docs.github.com/en/code-security/security-advisories/guidance-on-reporting-and-writing/privately-reporting-a-security-vulnerability),
via this repository's **Security** tab → **Report a vulnerability**.

If you cannot use that channel, email **ch.schymura@gmail.com** instead. Please don't open a
public issue for a suspected vulnerability.

Include what you'd include in any good bug report: the affected component or file, the
version or commit, reproduction steps, and the impact as you understand it.

## Scope

Lakana is pre-1.0. It implements grants and role templates, row filters and column masks,
personal-access and task tokens, credential-scoped query isolation, confinement of storage
paths and locations, and an audit log. Its threat model is written down:
`docs/security/threat-model.md` lists what is defended, which control does it and which gaps
are known, and, under "Explicitly not defended", what is not.

In scope:

- Anything that lets a principal read, write or change data, metadata, credentials or
  secrets beyond what its grants allow: the authorization and grant checks, row filters and
  column masks, task-token and personal-access-token handling, the isolation of a query by
  credential scoping, and the audit log.
- Anything that lets one environment or tenant reach another's data or credentials.
- Path and location confinement in storage, the artifact store and git fetching. A path
  string that escapes its root is in scope; a symlink planted inside a local-filesystem
  storage root is not (see below).
- Supply-chain issues in this repository's own code or workflows, and secrets or credentials
  accidentally committed to the repository.

Out of scope, because the threat model lists it as not defended (reporting it is still
welcome context, but it will not be treated as a new finding):

- Hostile code in a repository that Lakana is linked to. Python tasks run as trusted code
  as the worker's user; Lakana is not a sandbox.
- Inference from a trained model or from aggregate queries, and code inside a pickled
  artifact.
- A compromised platform administrator, and compromise of the host, the database or the
  object storage itself.
- A client that holds its own credential to the underlying storage and reads files directly,
  including an Iceberg REST client holding its own storage credentials.
- A symlink planted inside a local-filesystem storage root that points outside it (object
  stores have no symlinks).
- Failed-authentication rate limiting that is not proxy-aware, and an attacker who rotates
  source addresses.
- How far a principal holding `MANAGE_GRANT`, `MANAGE_TOKEN` or `MANAGE_GROUP` through an
  ordinary grant may delegate it, and the commit-time check of a table's data files (the
  worker's read-time check is the control).
- SeaweedFS's internal HTTP APIs inside the Compose team bundle.
- Per-principal query limits, sharing a query result between principals, and applying
  Iceberg row-level deletes written by another engine.

The threat model's "Explicitly not defended" section is the full and current list; if it
and this one differ, the threat model is right.

## Response expectations

This is a single-maintainer, pre-1.0 open-source project. There is no SLA and no time
commitment, before or after v1.0.0 (see [SUPPORT.md](SUPPORT.md)). Reports are read
on a best-effort basis; response and fix times vary with maintainer availability. Coordinated disclosure is welcome —
say so in your report if you'd like to discuss timing before anything is made public.
