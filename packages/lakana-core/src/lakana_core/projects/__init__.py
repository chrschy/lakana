"""Projects: the pure logic that turns a repository checkout into a manifest.

Nothing in this package runs git, opens a network connection or touches the
database except `repositories` and `deploys`, which own the deploy tables. The
worker (`lakana_worker.deploy`) feeds a checkout through `discovery`, `parse`,
`dag`, `manifest` and `diff`.
"""
