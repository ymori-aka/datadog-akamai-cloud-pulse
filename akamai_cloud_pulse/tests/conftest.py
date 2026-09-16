# (C) Datadog, Inc. 2026-present
# All rights reserved
# Licensed under a 3-clause BSD style license (see LICENSE)
import json
import os
import time
from collections.abc import Iterator
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import pytest
import requests

from datadog_checks.base.types import InstanceType
from datadog_checks.dev.http import MockResponse

FIXTURES = Path(__file__).parent / 'fixtures'

# Value reported for every complete bucket; the newest (partial) bucket reports 0.
COMPLETE_VALUE = 42.0

DIMENSION_VALUES = {
    'node_type': 'primary',
    'port': '443',
    'protocol': 'tcp',
    'config_id': '9001',
}


def load_fixture(name: str) -> Any:
    return json.loads((FIXTURES / name).read_text())


@pytest.fixture(scope='session')
def dd_environment() -> Iterator[InstanceType]:
    # The e2e environment talks to the real Cloud Pulse API.
    token = os.environ.get('AKAMAI_CLOUD_PULSE_PAT')
    if not token:
        pytest.skip('AKAMAI_CLOUD_PULSE_PAT is not set')
    yield {
        'personal_access_token': token,
        'services': [{'service_type': 'dbaas'}, {'service_type': 'nodebalancer'}],
        'empty_default_hostname': True,
    }


@pytest.fixture
def instance() -> InstanceType:
    return {
        'personal_access_token': 'test-pat',
        'services': [{'service_type': 'dbaas'}, {'service_type': 'nodebalancer'}],
        'tags': ['team:test'],
    }


class FakeCloudPulse:
    """Routes the check's HTTP calls to canned Linode / Cloud Pulse responses."""

    def __init__(self) -> None:
        self.requests: list[tuple[str, str, dict[str, Any] | None, dict[str, str]]] = []
        self.pat_status = 200
        self.metrics_401_once = False
        self.issued_tokens = 0
        self.failing_region: str | None = None
        self.regions = {
            item['id']: item['region']
            for name in ('databases_instances.json', 'nodebalancers.json')
            for item in load_fixture(name)['data']
        }

    def get(self, url: str, **kwargs: Any) -> MockResponse:
        return self._handle('get', url, None, kwargs)

    def post(self, url: str, **kwargs: Any) -> MockResponse:
        return self._handle('post', url, kwargs.get('json'), kwargs)

    def _handle(self, method: str, url: str, body: dict[str, Any] | None, kwargs: dict[str, Any]) -> MockResponse:
        headers = dict(kwargs.get('headers') or {})
        self.requests.append((method, url, body, headers))
        path = urlparse(url).path
        auth = headers.get('Authorization', '')

        if urlparse(url).netloc == 'api.linode.com':
            if self.pat_status != 200:
                return _response(url, {'errors': [{'reason': 'Invalid Token'}]}, self.pat_status)
            assert auth == 'Bearer test-pat'
            if path == '/v4/databases/instances':
                return _response(url, load_fixture('databases_instances.json'))
            if path == '/v4/nodebalancers':
                return _response(url, load_fixture('nodebalancers.json'))
            if path.endswith('/metric-definitions'):
                service_type = path.split('/')[4]
                return _response(url, load_fixture(f'{service_type}_metric_definitions.json'))
            if path.endswith('/token'):
                self.issued_tokens += 1
                return _response(url, {'token': f'cp-token-{self.issued_tokens}'})

        if urlparse(url).netloc == 'monitor-api.linode.com' and path.endswith('/metrics'):
            if self.metrics_401_once:
                self.metrics_401_once = False
                return _response(url, {'errors': [{'reason': 'Invalid Token'}]}, 401)
            assert auth.startswith('Bearer cp-token-')
            if len(body['metrics']) > 5:
                return _response(url, {'errors': [{'reason': 'Maximum limit of 5 metrics exceeded'}]}, 400)
            regions = {self.regions.get(i) for i in body['entity_ids']}
            if len(regions) > 1:
                return _response(url, {'errors': [{'reason': 'Entities belong to different data centers'}]}, 403)
            if self.failing_region is not None and self.failing_region in regions:
                return _response(url, {'errors': [{'reason': 'Service unavailable'}]}, 503)
            return _response(url, _metrics_payload(body))

        return _response(url, {'errors': [{'reason': 'Not found'}]}, 404)


def _metrics_payload(body: dict[str, Any]) -> dict[str, Any]:
    now = int(time.time())
    step = body['time_granularity']['value'] * 60
    values = [[now - step * i - 3, str(COMPLETE_VALUE)] for i in range(3, 0, -1)]
    values.append([now - 3, '0'])

    result = []
    for metric in body['metrics']:
        for entity_id in body['entity_ids']:
            labels = {'metric_name': f"{metric['aggregate_function']}_{metric['name']}", 'entity_id': str(entity_id)}
            for dim in body['group_by']:
                if dim in DIMENSION_VALUES:
                    labels[dim] = DIMENSION_VALUES[dim]
            if 'node_type' in body['group_by']:
                labels['node_id'] = 'primary-1'
            result.append({'metric': labels, 'values': values})
    return {'data': {'result': result, 'resultType': 'matrix'}, 'isPartial': False, 'status': 'success'}


def _response(url: str, payload: Any, status: int = 200) -> MockResponse:
    response = MockResponse(json_data=payload, status_code=status)
    response.url = url
    return response


@pytest.fixture
def fake_api(monkeypatch: pytest.MonkeyPatch) -> FakeCloudPulse:
    fake = FakeCloudPulse()
    monkeypatch.setattr(requests.Session, 'get', lambda self, url, **kw: fake.get(url, **kw))
    monkeypatch.setattr(requests.Session, 'post', lambda self, url, **kw: fake.post(url, **kw))
    return fake
