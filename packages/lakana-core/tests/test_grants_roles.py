"""Tests for the role → action-set mapping."""

from itertools import pairwise

from lakana_core.auth import Action
from lakana_core.grants.roles import ROLE_ACTIONS, Role


def test_every_role_is_mapped() -> None:
    """Every Role member has an entry in ROLE_ACTIONS."""
    assert set(ROLE_ACTIONS.keys()) == set(Role)


def test_admin_has_every_action_but_reading_other_peoples_query_text() -> None:
    """The admin role is every Action but `READ_QUERY_TEXT`, not a hand-picked list."""
    assert ROLE_ACTIONS[Role.ADMIN] == frozenset(Action) - {Action.READ_QUERY_TEXT}


def test_the_role_ladder_is_strictly_increasing() -> None:
    """Each role's actions are a strict superset of the previous role's."""
    ladder = [Role.VIEWER, Role.ANALYST, Role.DATA_SCIENTIST, Role.DATA_ENGINEER, Role.ADMIN]
    for lower, higher in pairwise(ladder):
        assert ROLE_ACTIONS[lower] < ROLE_ACTIONS[higher]


def test_viewer_is_read_only() -> None:
    """Viewer cannot submit queries or write anything."""
    assert ROLE_ACTIONS[Role.VIEWER] == frozenset(
        {Action.READ_ENVIRONMENT, Action.READ_NAMESPACE, Action.READ_TABLE}
    )


def test_analyst_adds_submit_query() -> None:
    """Analyst is viewer plus SUBMIT_QUERY, nothing else."""
    assert ROLE_ACTIONS[Role.ANALYST] == ROLE_ACTIONS[Role.VIEWER] | {Action.SUBMIT_QUERY}


def test_data_scientist_can_write_but_not_manage_namespaces_or_drop_tables() -> None:
    """data_scientist gets CREATE_TABLE/WRITE_TABLE but no DDL or DROP_TABLE."""
    added = ROLE_ACTIONS[Role.DATA_SCIENTIST] - ROLE_ACTIONS[Role.ANALYST]
    assert added == {Action.CREATE_TABLE, Action.WRITE_TABLE}


def test_data_engineer_adds_namespace_ddl_and_drop_table() -> None:
    """data_engineer is data_scientist plus namespace lifecycle and DROP_TABLE."""
    added = ROLE_ACTIONS[Role.DATA_ENGINEER] - ROLE_ACTIONS[Role.DATA_SCIENTIST]
    assert added == {
        Action.CREATE_NAMESPACE,
        Action.MANAGE_NAMESPACE,
        Action.DROP_NAMESPACE,
        Action.DROP_TABLE,
    }
