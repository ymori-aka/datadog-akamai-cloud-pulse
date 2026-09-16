# (C) Datadog, Inc. 2026-present
# All rights reserved
# Licensed under a 3-clause BSD style license (see LICENSE)
from typing import Callable

import pytest

from datadog_checks.akamai_cloud_pulse import AkamaiCloudPulseCheck
from datadog_checks.akamai_cloud_pulse.check import _pick_aggregate, _scrape_seconds
from datadog_checks.base import AgentCheck
from datadog_checks.base.stubs.aggregator import AggregatorStub
from datadog_checks.base.types import InstanceType
from datadog_checks.dev.utils import get_metadata_metrics

from .conftest import COMPLETE_VALUE, FakeCloudPulse

pytestmark = pytest.mark.unit


def _metric_requests(fake: FakeCloudPulse) -> list[dict]:
    return [body for method, url, body, _ in fake.requests if url.endswith('/metrics')]


def test_check_collects_all_metrics(
    dd_run_check: Callable[..., None], aggregator: AggregatorStub, instance: InstanceType, fake_api: FakeCloudPulse
) -> None:
    check = AkamaiCloudPulseCheck('akamai_cloud_pulse', {}, [instance])
    dd_run_check(check)

    aggregator.assert_metrics_using_metadata(get_metadata_metrics())

    # Only the last complete bucket is submitted, never the partial one.
    aggregator.assert_metric(
        'akamai_cloud_pulse.dbaas.cpu_usage',
        value=COMPLETE_VALUE,
        count=1,
        tags=[
            'team:test',
            'entity_id:1001',
            'entity_label:orders-db',
            'region:us-ord',
            'engine:postgresql',
            'engine_version:18.6',
            'node_id:primary-1',
            'node_type:primary',
            'aggregate_function:avg',
        ],
    )
    aggregator.assert_metric(
        'akamai_cloud_pulse.nodebalancer.ingress_traffic_rate',
        value=COMPLETE_VALUE,
        count=1,
        tags=[
            'team:test',
            'entity_id:2001',
            'entity_label:lke100-abc',
            'region:us-ord',
            'lke_cluster_id:100',
            'lke_cluster:shop',
            'config_id:9001',
            'port:443',
            'protocol:tcp',
            'aggregate_function:sum',
        ],
    )
    aggregator.assert_metric_has_tag('akamai_cloud_pulse.nodebalancer.ingress_traffic_rate', 'entity_id:2002')
    # Suspended databases are not monitored.
    for metric in aggregator.metrics('akamai_cloud_pulse.dbaas.cpu_usage'):
        assert 'entity_id:1003' not in metric.tags

    for service_type in ('dbaas', 'nodebalancer'):
        aggregator.assert_service_check(
            'akamai_cloud_pulse.can_connect',
            AgentCheck.OK,
            tags=[f'service_type:{service_type}', 'team:test'],
            count=1,
        )
    # Every metric declared in metadata.csv is emitted.
    for name in get_metadata_metrics():
        aggregator.assert_metric(name, at_least=1)
    aggregator.assert_all_metrics_covered()


def test_requests_respect_api_limits(
    dd_run_check: Callable[..., None], instance: InstanceType, fake_api: FakeCloudPulse
) -> None:
    check = AkamaiCloudPulseCheck('akamai_cloud_pulse', {}, [instance])
    dd_run_check(check)

    bodies = _metric_requests(fake_api)
    # 7 dbaas metrics -> 2 requests (one region),
    # 15 nodebalancer metrics -> 3 requests x 2 regions.
    assert len(bodies) == 8
    for body in bodies:
        assert len(body['metrics']) <= 5
        assert body['group_by'][0] == 'entity_id'
        for metric in body['metrics']:
            if metric['name'] == 'cpu_usage':
                assert metric['aggregate_function'] == 'avg'
            if metric['name'] == 'nb_ingress_traffic_rate':
                assert metric['aggregate_function'] == 'sum'
    nb = [b for b in bodies if b['metrics'][0]['name'].startswith('nb_')]
    assert all(b['time_granularity'] == {'unit': 'min', 'value': 5} for b in nb)
    # One token per service and region, reused across requests.
    assert fake_api.issued_tokens == 3


def test_failing_region_does_not_block_others(
    dd_run_check: Callable[..., None], aggregator: AggregatorStub, fake_api: FakeCloudPulse
) -> None:
    fake_api.failing_region = 'us-sea'
    instance = {'personal_access_token': 'test-pat', 'services': [{'service_type': 'nodebalancer'}]}
    check = AkamaiCloudPulseCheck('akamai_cloud_pulse', {}, [instance])
    dd_run_check(check)

    aggregator.assert_metric_has_tag('akamai_cloud_pulse.nodebalancer.ingress_traffic_rate', 'entity_id:2001')
    for metric in aggregator.metrics('akamai_cloud_pulse.nodebalancer.ingress_traffic_rate'):
        assert 'entity_id:2002' not in metric.tags
    aggregator.assert_service_check('akamai_cloud_pulse.can_connect', AgentCheck.CRITICAL, count=1)


def test_second_run_is_throttled(
    dd_run_check: Callable[..., None], aggregator: AggregatorStub, instance: InstanceType, fake_api: FakeCloudPulse
) -> None:
    check = AkamaiCloudPulseCheck('akamai_cloud_pulse', {}, [instance])
    dd_run_check(check)
    first = len(_metric_requests(fake_api))
    aggregator.reset()

    dd_run_check(check)
    assert len(_metric_requests(fake_api)) == first
    assert not aggregator.metric_names
    aggregator.assert_service_check('akamai_cloud_pulse.can_connect', AgentCheck.OK, count=2)


def test_explicit_entities_and_metrics(
    dd_run_check: Callable[..., None], aggregator: AggregatorStub, fake_api: FakeCloudPulse
) -> None:
    instance = {
        'personal_access_token': 'test-pat',
        'services': [
            {'service_type': 'dbaas', 'entity_ids': [1002, 4242], 'metrics': ['cpu_usage', 'no_such_metric']},
        ],
    }
    check = AkamaiCloudPulseCheck('akamai_cloud_pulse', {}, [instance])
    dd_run_check(check)

    bodies = _metric_requests(fake_api)
    # 4242 is not discovered, so its region is unknown and it is queried on its own.
    assert sorted(b['entity_ids'] for b in bodies) == [[1002], [4242]]
    assert all([m['name'] for m in b['metrics']] == ['cpu_usage'] for b in bodies)

    aggregator.assert_metric('akamai_cloud_pulse.dbaas.cpu_usage', count=2)
    aggregator.assert_metric_has_tag('akamai_cloud_pulse.dbaas.cpu_usage', 'entity_label:cache')
    # An entity missing from discovery still gets its ID tag.
    aggregator.assert_metric_has_tag('akamai_cloud_pulse.dbaas.cpu_usage', 'entity_id:4242')
    aggregator.assert_all_metrics_covered()


def test_unauthorized_pat_reports_critical(
    dd_run_check: Callable[..., None], aggregator: AggregatorStub, instance: InstanceType, fake_api: FakeCloudPulse
) -> None:
    fake_api.pat_status = 401
    check = AkamaiCloudPulseCheck('akamai_cloud_pulse', {}, [instance])
    dd_run_check(check)

    assert not aggregator.metric_names
    aggregator.assert_service_check('akamai_cloud_pulse.can_connect', AgentCheck.CRITICAL, count=2)


def test_expired_token_is_renewed_once(
    dd_run_check: Callable[..., None], aggregator: AggregatorStub, fake_api: FakeCloudPulse
) -> None:
    fake_api.metrics_401_once = True
    instance = {
        'personal_access_token': 'test-pat',
        'services': [{'service_type': 'dbaas', 'metrics': ['cpu_usage']}],
    }
    check = AkamaiCloudPulseCheck('akamai_cloud_pulse', {}, [instance])
    dd_run_check(check)

    assert fake_api.issued_tokens == 2
    aggregator.assert_metric('akamai_cloud_pulse.dbaas.cpu_usage', value=COMPLETE_VALUE)
    aggregator.assert_service_check('akamai_cloud_pulse.can_connect', AgentCheck.OK)


def test_unsupported_service_type(dd_run_check: Callable[..., None], fake_api: FakeCloudPulse) -> None:
    instance = {'personal_access_token': 'test-pat', 'services': [{'service_type': 'linode'}]}
    check = AkamaiCloudPulseCheck('akamai_cloud_pulse', {}, [instance])
    with pytest.raises(Exception, match='Unsupported service_type'):
        dd_run_check(check)


@pytest.mark.parametrize(
    'value, expected',
    [('60s', 60), ('300s', 300), ('5m', 300), ('1h', 3600), ('120', 120), (None, 60), ('bogus', 60)],
)
def test_scrape_seconds(value, expected) -> None:
    assert _scrape_seconds({'scrape_interval': value}) == expected


@pytest.mark.parametrize(
    'available, expected',
    [(['avg'], 'avg'), (['sum'], 'sum'), (['max', 'min', 'sum', 'avg'], 'avg'), (['max', 'min'], 'max'), ([], 'avg')],
)
def test_pick_aggregate(available, expected) -> None:
    assert _pick_aggregate({'available_aggregate_functions': available}) == expected
