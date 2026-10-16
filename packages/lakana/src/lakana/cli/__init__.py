"""The `lakana` command-line interface, built on the SDK.

Kept out of `lakana/__init__.py` on purpose: importing the SDK never loads
`typer` or this package. `rich` is not part of that guarantee — httpx's own
optional CLI imports `rich` whenever it's installed, regardless of anything
`lakana` does, so a bare `import lakana` can still end up with `rich` in
`sys.modules`. That's httpx's behaviour, not a leak of the CLI stack.
"""
