"""Resource classes for the platform API's resources.

Each module here corresponds to one platform API resource — data-plane
(environments, namespaces, tables, queries) or admin (principals, groups,
tokens, task tokens, grants, row filters, column masks, secrets, audit
events) — and holds the `Client` attribute of the same name.
"""

from __future__ import annotations
