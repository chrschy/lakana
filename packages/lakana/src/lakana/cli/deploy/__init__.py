"""`lakana bundle init`: generate the team Compose bundle.

The package keeps its `deploy` name because generated bundles run
`python -m lakana.cli.deploy.setup`.
"""

from __future__ import annotations

from lakana.cli.deploy._cli import register
from lakana.cli.deploy._init import TEMPLATE_NAME

__all__ = ["TEMPLATE_NAME", "register"]
