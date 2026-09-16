# (C) Datadog, Inc. 2026-present
# All rights reserved
# Licensed under a 3-clause BSD style license (see LICENSE)
from typing import Any

import pytest

from datadog_checks.base import AgentCheck
from datadog_checks.base.types import InstanceType
from datadog_checks.dev.utils import get_metadata_metrics


@pytest.mark.e2e
def test_e2e(dd_agent_check: Any, dd_environment: InstanceType) -> None:
    # Requires AKAMAI_CLOUD_PULSE_PAT and an account with at least one database
    # and one NodeBalancer. Which metrics appear depends on that account (for
    # example UDP metrics only exist for UDP configs), so only check the ones
    # that are emitted.
    aggregator = dd_agent_check(dd_environment)

    aggregator.assert_metrics_using_metadata(get_metadata_metrics(), check_submission_type=True)
    assert aggregator.metrics('akamai_cloud_pulse.dbaas.cpu_usage')
    assert aggregator.metrics('akamai_cloud_pulse.nodebalancer.ingress_traffic_rate')
    aggregator.assert_service_check('akamai_cloud_pulse.can_connect', AgentCheck.OK)
