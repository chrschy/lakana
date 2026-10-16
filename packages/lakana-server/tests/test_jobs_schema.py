"""The documented states of a job match the ones the service reports."""

from __future__ import annotations

from lakana_core.jobs import service
from lakana_server.api.platform.schemas import JobOut

_STATES = [
    service.STATE_SCHEDULED,
    service.STATE_PAUSED,
    service.STATE_WAITING,
    service.STATE_STARTING,
    service.STATE_NO_SCHEDULE,
    service.STATE_NO_RUN_PRINCIPAL,
    service.STATE_RUN_PRINCIPAL_UNUSABLE,
]
_CODES = ["job_waits", "job_starts_soon", "no_run_principal", "run_principal_unusable"]


def test_the_api_schema_lists_every_state_and_wait_code() -> None:
    text = JobOut.model_json_schema().get("description", "") + JobOut.__doc__
    for word in [*_STATES, *_CODES]:
        assert word in text, word
