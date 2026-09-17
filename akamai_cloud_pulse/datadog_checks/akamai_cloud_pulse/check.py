# (C) Datadog, Inc. 2026-present
# All rights reserved
# Licensed under a 3-clause BSD style license (see LICENSE)
from __future__ import annotations

import math
import time
from typing import Any

from requests.exceptions import HTTPError, RequestException

from datadog_checks.base import AgentCheck, ConfigurationError
from datadog_checks.base.types import InitConfigType, InstanceType

from .config_models import ConfigMixin

# Cloud Pulse rejects metric requests with more than 5 metrics.
MAX_METRICS_PER_REQUEST = 5
# The token endpoint accepts at most 100 entity IDs.
MAX_ENTITIES_PER_TOKEN = 100
# Tokens are documented to live for 6 hours; renew well before that.
TOKEN_TTL_SECONDS = 5 * 60 * 60
DEFAULT_API_URL = 'https://api.linode.com/v4'
DEFAULT_MONITOR_API_URL = 'https://monitor-api.linode.com/v2'
DEFAULT_ENTITY_REFRESH_INTERVAL = 3600
# Preferred aggregate when a metric supports several.
AGGREGATE_PREFERENCE = ('avg', 'sum', 'max', 'min')

# Per service:
#   list_path       where to list entities
#   id_field        entity field Cloud Pulse uses as entity_id
#   metric_prefix   prefix to strip from Cloud Pulse metric names
#   regional        metrics are queried with entity_region, and the token covers
#                   the whole account (Object Storage rejects entity_ids on the token)
SERVICES: dict[str, dict[str, Any]] = {
    'dbaas': {
        'list_path': '/databases/instances',
        'id_field': 'id',
        'metric_prefix': '',
        'regional': False,
    },
    'nodebalancer': {
        'list_path': '/nodebalancers',
        'id_field': 'id',
        'metric_prefix': 'nb_',
        'regional': False,
    },
    'objectstorage': {
        'list_path': '/object-storage/buckets',
        'id_field': 'hostname',
        'metric_prefix': 'obj_',
        'regional': True,
    },
}


class CloudPulseAuthError(Exception):
    """The personal access token was rejected."""


class AkamaiCloudPulseCheck(AgentCheck, ConfigMixin):
    __NAMESPACE__ = 'akamai_cloud_pulse'

    SERVICE_CHECK_CAN_CONNECT = 'can_connect'

    def __init__(self, name: str, init_config: InitConfigType, instances: list[InstanceType]) -> None:
        super().__init__(name, init_config, instances)
        # service_type -> list of metric definitions
        self._definitions: dict[str, list[dict[str, Any]]] = {}
        # service_type -> {entity_id: [tags]}; entity IDs are kept as strings
        self._entities: dict[str, dict[str, list[str]]] = {}
        self._entities_refreshed_at: dict[str, float] = {}
        # (service_type, entity_ids) -> (token, expires_at)
        self._tokens: dict[tuple[str, tuple[str, ...]], tuple[str, float]] = {}
        # (service_type, scrape_seconds) -> earliest time the next query is worthwhile
        self._next_query_at: dict[tuple[str, int], float] = {}
        self._api_url = DEFAULT_API_URL
        self._monitor_api_url = DEFAULT_MONITOR_API_URL
        self._entity_refresh_interval = DEFAULT_ENTITY_REFRESH_INTERVAL
        self.check_initializations.append(self._initialize)

    def _initialize(self) -> None:
        self._api_url = (self.config.api_url or DEFAULT_API_URL).rstrip('/')
        self._monitor_api_url = (self.config.monitor_api_url or DEFAULT_MONITOR_API_URL).rstrip('/')
        self._entity_refresh_interval = self.config.entity_refresh_interval or DEFAULT_ENTITY_REFRESH_INTERVAL
        for service in self.config.services:
            if service.service_type not in SERVICES:
                raise ConfigurationError(
                    f'Unsupported service_type `{service.service_type}`. Supported: {", ".join(sorted(SERVICES))}'
                )
            if service.entity_regions and not SERVICES[service.service_type]['regional']:
                raise ConfigurationError(
                    f'`entity_regions` is only supported for objectstorage, not {service.service_type}'
                )

    def check(self, _: InstanceType) -> None:
        for service in self.config.services:
            service_type = service.service_type
            sc_tags = [f'service_type:{service_type}', *(self.config.tags or ())]
            try:
                self._collect_service(service)
            except CloudPulseAuthError as e:
                self.log.warning('Cloud Pulse rejected the credentials for %s: %s', service_type, e)
                self.service_check(self.SERVICE_CHECK_CAN_CONNECT, AgentCheck.CRITICAL, tags=sc_tags, message=str(e))
            except (HTTPError, RequestException, ValueError) as e:
                self.log.warning('Failed to collect Cloud Pulse metrics for %s: %s', service_type, e)
                self.service_check(self.SERVICE_CHECK_CAN_CONNECT, AgentCheck.CRITICAL, tags=sc_tags, message=str(e))
            else:
                self.service_check(self.SERVICE_CHECK_CAN_CONNECT, AgentCheck.OK, tags=sc_tags)

    # ---- collection -------------------------------------------------------

    def _collect_service(self, service: Any) -> None:
        service_type = service.service_type
        now = time.time()

        definitions = self._metric_definitions(service_type)
        entities = self._resolve_entities(service, now)
        if not entities:
            self.log.info('No %s entities to monitor', service_type)
            return

        if service.metrics:
            wanted = set(service.metrics)
            unknown = wanted - {d['metric'] for d in definitions}
            if unknown:
                self.log.warning('Unknown %s metrics ignored: %s', service_type, ', '.join(sorted(unknown)))
            definitions = [d for d in definitions if d['metric'] in wanted]

        # Metrics with different scrape intervals are queried separately, each at
        # most once per interval: hourly metrics (for example Object Storage bucket
        # size) would otherwise be fetched and submitted again every run.
        by_interval: dict[int, list[dict[str, Any]]] = {}
        for definition in definitions:
            by_interval.setdefault(_scrape_seconds(definition), []).append(definition)

        # Cloud Pulse rejects a request whose entities span regions
        # ("Entities belong to different data centers"), so query each region separately.
        by_region: dict[str, list[str]] = {}
        for entity_id, tags in sorted(entities.items()):
            by_region.setdefault(_region_of(tags), []).append(entity_id)
        if SERVICES[service_type]['regional'] and '' in by_region:
            # entity_region is mandatory for these services.
            self.log.warning(
                'Skipping %s entities with an unknown region: %s', service_type, ', '.join(by_region.pop(''))
            )

        # One failing region should not stop the others; report the first error afterwards.
        first_error: Exception | None = None
        for scrape_seconds, defs in sorted(by_interval.items()):
            throttle_key = (service_type, scrape_seconds)
            if now < self._next_query_at.get(throttle_key, 0):
                continue
            granularity_min = max(1, math.ceil(scrape_seconds / 60))
            for i in range(0, len(defs), MAX_METRICS_PER_REQUEST):
                batch = defs[i : i + MAX_METRICS_PER_REQUEST]
                for region, region_ids in sorted(by_region.items()):
                    for j in range(0, len(region_ids), MAX_ENTITIES_PER_TOKEN):
                        ids = tuple(region_ids[j : j + MAX_ENTITIES_PER_TOKEN])
                        try:
                            self._query_and_submit(service_type, region, ids, batch, granularity_min, entities, now)
                        except CloudPulseAuthError:
                            raise
                        except (HTTPError, RequestException, ValueError) as e:
                            self.log.warning('Cloud Pulse %s query failed for region %s: %s', service_type, region, e)
                            first_error = first_error or e
            self._next_query_at[throttle_key] = now + max(60, scrape_seconds)

        if first_error is not None:
            raise first_error

    def _query_and_submit(
        self,
        service_type: str,
        region: str,
        entity_ids: tuple[str, ...],
        definitions: list[dict[str, Any]],
        granularity_min: int,
        entities: dict[str, list[str]],
        now: float,
    ) -> None:
        by_name = {d['metric']: d for d in definitions}
        group_by = ['entity_id']
        for definition in definitions:
            for dim in definition.get('dimensions') or []:
                label = dim['dimension_label']
                if label not in group_by:
                    group_by.append(label)

        body: dict[str, Any] = {
            'metrics': [{'name': d['metric'], 'aggregate_function': _pick_aggregate(d)} for d in definitions],
            'relative_time_duration': {'unit': 'min', 'value': granularity_min * 3},
            'time_granularity': {'unit': 'min', 'value': granularity_min},
            'group_by': group_by,
        }
        if SERVICES[service_type]['regional']:
            body['entity_region'] = region
            body['entity_ids'] = list(entity_ids)
        else:
            body['entity_ids'] = [int(i) for i in entity_ids]
        result = self._fetch_metrics(service_type, entity_ids, body)

        # The newest bucket is usually still being filled (it can read 0), so
        # only submit points that are at least one full bucket old.
        cutoff = now - granularity_min * 60
        prefix = SERVICES[service_type]['metric_prefix']
        for series in result:
            labels = dict(series.get('metric') or {})
            raw_name = labels.pop('metric_name', '')
            aggregate, _, cp_name = raw_name.partition('_')
            if cp_name not in by_name:
                self.log.debug('Skipping unexpected series %s', raw_name)
                continue

            point = _latest_point(series.get('values') or [], cutoff)
            if point is None:
                continue

            entity_id = labels.pop('entity_id', None)
            tags = list(self.config.tags or ())
            if entity_id is not None:
                tags.extend(entities.get(str(entity_id), [f'entity_id:{entity_id}']))
            tags.extend(f'{k}:{v}' for k, v in sorted(labels.items()) if v != '')
            tags.append(f'aggregate_function:{aggregate}')

            name = cp_name[len(prefix) :] if prefix and cp_name.startswith(prefix) else cp_name
            self.gauge(f'{service_type}.{name}', point, tags=tags)

    def _fetch_metrics(self, service_type: str, entity_ids: tuple[str, ...], body: dict[str, Any]) -> list[Any]:
        url = f'{self._monitor_api_url}/monitor/services/{service_type}/metrics'
        token_key = self._token_key(service_type, entity_ids)
        for attempt in (1, 2):
            token = self._token(*token_key)
            response = self.http.post(url, json=body, extra_headers={'Authorization': f'Bearer {token}'})
            if response.status_code == 401 and attempt == 1:
                # Token expired early or was revoked: issue a new one and retry once.
                self._tokens.pop(token_key, None)
                continue
            _raise_for_status(response)
            payload = response.json()
            if payload.get('isPartial'):
                self.log.debug('Partial Cloud Pulse response for %s', service_type)
            return (payload.get('data') or {}).get('result') or []
        return []

    # ---- Linode API -------------------------------------------------------

    def _api_get(self, path: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        response = self.http.get(
            f'{self._api_url}{path}',
            params=params,
            extra_headers={'Authorization': f'Bearer {self.config.personal_access_token}'},
        )
        _raise_for_status(response)
        return response.json()

    def _api_get_all(self, path: str) -> list[dict[str, Any]]:
        items: list[dict[str, Any]] = []
        page, pages = 1, 1
        while page <= pages:
            payload = self._api_get(path, {'page': page, 'page_size': 500})
            items.extend(payload.get('data') or [])
            pages = payload.get('pages') or 1
            page += 1
        return items

    def _metric_definitions(self, service_type: str) -> list[dict[str, Any]]:
        if service_type not in self._definitions:
            self._definitions[service_type] = self._api_get_all(f'/monitor/services/{service_type}/metric-definitions')
        return self._definitions[service_type]

    @staticmethod
    def _token_key(service_type: str, entity_ids: tuple[str, ...]) -> tuple[str, tuple[str, ...]]:
        # An account-wide token (regional services) is shared by every request.
        return (service_type, () if SERVICES[service_type]['regional'] else entity_ids)

    def _token(self, service_type: str, entity_ids: tuple[str, ...]) -> str:
        key = (service_type, entity_ids)
        cached = self._tokens.get(key)
        if cached and cached[1] > time.time():
            return cached[0]

        body: dict[str, Any] = {}
        if entity_ids:
            body['entity_ids'] = [int(i) for i in entity_ids]
        response = self.http.post(
            f'{self._api_url}/monitor/services/{service_type}/token',
            json=body,
            extra_headers={'Authorization': f'Bearer {self.config.personal_access_token}'},
        )
        _raise_for_status(response)
        token = response.json()['token']
        self._tokens[key] = (token, time.time() + TOKEN_TTL_SECONDS)
        return token

    def _resolve_entities(self, service: Any, now: float) -> dict[str, list[str]]:
        service_type = service.service_type
        refreshed_at = self._entities_refreshed_at.get(service_type)
        if refreshed_at is not None and now - refreshed_at < self._entity_refresh_interval:
            return self._entities[service_type]

        try:
            items = self._api_get_all(SERVICES[service_type]['list_path'])
        except CloudPulseAuthError:
            raise
        except (HTTPError, RequestException, ValueError) as e:
            if not service.entity_ids:
                raise
            # Discovery is only needed for tags when IDs are configured explicitly.
            self.log.warning('Could not list %s entities, continuing without entity tags: %s', service_type, e)
            items = []

        id_field = SERVICES[service_type]['id_field']
        discovered = {}
        for item in items:
            if service_type == 'dbaas' and item.get('status') not in (None, 'active'):
                continue
            if not item.get(id_field):
                continue
            discovered[str(item[id_field])] = _entity_tags(service_type, item)

        if service.entity_ids:
            wanted_ids = [str(i) for i in service.entity_ids]
            entities = {i: discovered.get(i, [f'entity_id:{i}']) for i in wanted_ids}
        else:
            entities = discovered

        if service.entity_regions:
            regions = set(service.entity_regions)
            entities = {i: tags for i, tags in entities.items() if _region_of(tags) in regions}

        self._entities[service_type] = entities
        self._entities_refreshed_at[service_type] = now
        return entities


def _entity_tags(service_type: str, item: dict[str, Any]) -> list[str]:
    tags = [f'entity_id:{item[SERVICES[service_type]["id_field"]]}']
    if item.get('region'):
        tags.append(f'region:{item["region"]}')
    if service_type == 'objectstorage':
        if item.get('label'):
            tags.append(f'bucket:{item["label"]}')
        return tags

    if item.get('label'):
        tags.append(f'entity_label:{item["label"]}')
    if service_type == 'dbaas':
        if item.get('engine'):
            tags.append(f'engine:{item["engine"]}')
        if item.get('version'):
            tags.append(f'engine_version:{item["version"]}')
    elif service_type == 'nodebalancer':
        cluster = item.get('lke_cluster') or {}
        if cluster.get('id'):
            tags.append(f'lke_cluster_id:{cluster["id"]}')
        if cluster.get('label'):
            tags.append(f'lke_cluster:{cluster["label"]}')
    return tags


def _region_of(tags: list[str]) -> str:
    for tag in tags:
        if tag.startswith('region:'):
            return tag[len('region:') :]
    return ''


def _scrape_seconds(definition: dict[str, Any]) -> int:
    value = str(definition.get('scrape_interval') or '60s').strip()
    units = {'s': 1, 'm': 60, 'h': 3600}
    try:
        if value[-1] in units:
            return int(value[:-1]) * units[value[-1]]
        return int(value)
    except ValueError:
        return 60


def _pick_aggregate(definition: dict[str, Any]) -> str:
    available = definition.get('available_aggregate_functions') or []
    for aggregate in AGGREGATE_PREFERENCE:
        if aggregate in available:
            return aggregate
    return available[0] if available else 'avg'


def _latest_point(values: list[Any], cutoff: float) -> float | None:
    """Return the newest non-null value at or before `cutoff`."""
    latest_ts = None
    latest_value = None
    for ts, value in values:
        ts = float(ts)
        if ts > cutoff or value is None:
            continue
        if latest_ts is None or ts > latest_ts:
            try:
                latest_value = float(value)
            except (TypeError, ValueError):
                continue
            latest_ts = ts
    return latest_value


def _raise_for_status(response: Any) -> None:
    if response.status_code == 401:
        raise CloudPulseAuthError(f'Unauthorized ({response.url}): check the personal access token and its scopes')
    try:
        response.raise_for_status()
    except HTTPError as e:
        detail = ''
        try:
            errors = response.json().get('errors') or []
            detail = '; '.join(err.get('reason', '') for err in errors)
        except ValueError:
            pass
        raise HTTPError(f'{e} {detail}'.strip(), response=response) from e
