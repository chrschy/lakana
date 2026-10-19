# Network settings of the worker

How the worker reaches git remotes and webhook addresses through a proxy or with an internal CA,
and what it refuses to reach. Where the settings go in a Compose bundle:
[Deploy with Compose](../how-to/deploy-with-compose.md). Every other setting:
[Settings](settings.md).

## Git network settings

The worker runs git with a minimal environment, so its own `HTTPS_PROXY`, `NO_PROXY` and CA
variables are **not** seen by git. For `https` repositories, set these on the **worker only**
(`LAKANA_GIT__*`); the server and `migrate` need neither the variables nor the CA file. They load
the same settings and check only the format, the worker checks the file at startup and refuses
to start if it cannot read it.

| Setting | Meaning |
|---|---|
| `LAKANA_GIT__HTTPS_PROXY` | `http://` or `https://` proxy URL; git tunnels through it (`CONNECT`). Credentials go in the URL as percent-encoded `user:password` (`http://svc:p%40ss@proxy.corp:3128`); a username without a password, or an unencoded `@`, is refused. They are redacted from errors and logs. A proxy URL with a query string is refused: put the credential in the userinfo. A path segment that looks like a token is warned about at startup, never refused: only userinfo is redacted from logs. For an `https://` proxy the CA bundle is used for the proxy too. |
| `LAKANA_GIT__NO_PROXY` | Comma-separated hosts, domains or CIDRs that bypass the proxy (passed as git's `no_proxy`). |
| `LAKANA_GIT__CA_BUNDLE` | Absolute path of a PEM file of trusted CAs, for an internal GitLab with a private CA. It **replaces** the system store for git, so include every CA your repositories need. Must exist and be readable in the worker. |

Nothing else is inherited. SSH repositories ignore all three; `ProxyCommand` is not supported
(use an `https` repository, or a host reachable directly). A proxy password is visible to git
as command-scope config for the duration of a fetch, like the repository URL.

HTTPS repositories need git 2.47.2 or later (or a distribution build carrying the
CVE-2024-50349 fix): the worker answers only git's exact credential prompts, which older git
prints with the username unencoded. On an older git, a username with a reserved character such
as `gitlab+deploy-token-1` fails with an authentication error. The Lakana image ships a current
git; this matters for local mode, where your own git runs.

## Webhook network settings

The worker sends a webhook only to a **public** `https` address on port 443. It looks up the name
once, refuses the request if any address is private, loopback, link-local (including the cloud
metadata address `169.254.169.254`), shared (`100.64.0.0/10`), multicast, reserved or an IPv6
form of one of those, and then connects to the address it checked, so a changed DNS answer
cannot move the request. It follows no redirect (a redirect is a permanent failure) and ignores
`HTTPS_PROXY` and the other environment variables. The URL is never written to a log, a
delivery row or an error: a failure shows a fixed reason such as `timeout` or `TLS error`. An
address it refuses shows `the webhook host is a private or internal address`.

Set these on the **worker only** (`LAKANA_WEBHOOK__*`); the server and `migrate` load the same
settings and check only the format.

| Setting | Meaning |
|---|---|
| `LAKANA_WEBHOOK__ALLOW_HOSTS` | Comma-separated host names, or `*.domain` for every subdomain of a domain (not the domain itself), that skip the address check and may use `http` and any port, for a chat server inside your network (`chat.corp.example,*.hooks.corp.example`). Case and a trailing dot do not matter. An allowed name skips **every** address check, so list only names whose DNS you control and never a `*.domain` where others can create records. An IP address cannot be listed. |
| `LAKANA_WEBHOOK__PROXY` | `http://` or `https://` proxy URL; the webhook tunnels through it (`CONNECT`). The worker still checks the destination name, but the proxy resolves it again, so the **proxy is trusted** to refuse internal addresses: deny private, loopback, link-local and `100.64.0.0/10` destinations in its access rules (in squid, an `acl ... dst` list with `http_access deny`). The worker must still resolve the name itself. An `https` proxy is verified with the CA bundle below. Without this setting there is no proxy, whatever `HTTPS_PROXY` says. Credentials go in the URL as for `LAKANA_GIT__HTTPS_PROXY`. |
| `LAKANA_WEBHOOK__CA_BUNDLE` | Absolute path of a PEM file of trusted CAs, for an internal server with a private CA. It **replaces** the built-in store and never disables verification. Must exist and be readable in the worker. |
