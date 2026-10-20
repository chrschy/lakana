"""The five role templates and their action bundles."""

from __future__ import annotations

import enum

from lakana_core.auth import Action


class Role(enum.StrEnum):
    """A named bundle of Actions, so most grants never need to list raw actions.

    Custom (user-defined) roles are deliberately not supported.
    These five are a fixed, server-side constant.
    """

    VIEWER = "VIEWER"
    ANALYST = "ANALYST"
    DATA_SCIENTIST = "DATA_SCIENTIST"
    DATA_ENGINEER = "DATA_ENGINEER"
    ADMIN = "ADMIN"


_VIEWER_ACTIONS = frozenset({Action.READ_ENVIRONMENT, Action.READ_NAMESPACE, Action.READ_TABLE})
_ANALYST_ACTIONS = _VIEWER_ACTIONS | {Action.SUBMIT_QUERY}
_DATA_SCIENTIST_ACTIONS = _ANALYST_ACTIONS | {Action.CREATE_TABLE, Action.WRITE_TABLE}
_DATA_ENGINEER_ACTIONS = _DATA_SCIENTIST_ACTIONS | {
    Action.CREATE_NAMESPACE,
    Action.MANAGE_NAMESPACE,
    Action.DROP_NAMESPACE,
    Action.DROP_TABLE,
}

ROLE_ACTIONS: dict[Role, frozenset[Action]] = {
    Role.VIEWER: _VIEWER_ACTIONS,
    Role.ANALYST: _ANALYST_ACTIONS,
    Role.DATA_SCIENTIST: _DATA_SCIENTIST_ACTIONS,
    Role.DATA_ENGINEER: _DATA_ENGINEER_ACTIONS,
    Role.ADMIN: frozenset(Action) - {Action.READ_QUERY_TEXT},
}
"""What each role can do.

`admin` holds every action but `READ_QUERY_TEXT`: seeing other people's SQL text in the audit
log is granted to a person on its own (#88), so that administering the instance does not by
itself show what everyone queried.

`data_scientist` sits between `analyst` and `data_engineer`: it can persist
engineered features, model outputs, and predictions into existing
tables/namespaces (`CREATE_TABLE`, `WRITE_TABLE`), but cannot create, alter,
or drop namespaces, nor drop tables — those stay a `data_engineer`/
governance action. ML engineers are covered by the same role; if their
needs diverge from data scientists' in practice, that is a one-line
addition here, not a redesign.
"""
