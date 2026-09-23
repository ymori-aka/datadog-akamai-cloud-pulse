# Akamai Cloud Pulse API notes

Observed 2026-09-16 against the public Cloud Pulse API.

## Flow

1. `GET https://api.linode.com/v4/monitor/services` — service types available to the account.
   Observed: `dbaas`, `nodebalancer`, `objectstorage`, `logs`. Response includes
   `alert.polling_interval_seconds` (min 300).
2. `GET https://api.linode.com/v4/monitor/services/{service_type}/metric-definitions`
   — fields: `metric`, `label`, `unit`, `metric_type`, `scrape_interval`, `is_alertable`,
   `available_aggregate_functions`, `dimensions[].dimension_label`.
3. `POST https://api.linode.com/v4/monitor/services/{service_type}/token`
   body `{"entity_ids": [...]}` (max 100, min 1; objectstorage accepts `{}`).
   Returns `{"token": "..."}`. Opaque (not a JWT). Documented lifetime: 6 hours.
   Scope: `monitor:read_only` + read access to every entity listed.
4. `POST https://monitor-api.linode.com/v2/monitor/services/{service_type}/metrics`
   with `Authorization: Bearer <token from step 3>`.

`v4` and `v4beta` both work for steps 1-3. Use `v2` (GA) for step 4.

## Metrics request

```json
{
  "metrics": [{"name": "cpu_usage", "aggregate_function": "avg"}],
  "entity_ids": [1001],
  "relative_time_duration": {"unit": "min", "value": 15},
  "time_granularity": {"unit": "min", "value": 5},
  "group_by": ["entity_id", "node_type"]
}
```

Response is Prometheus-like (`resultType: matrix`):

- `metric.metric_name` is prefixed with the aggregate: `avg_cpu_usage`, `sum_nb_ingress_traffic_rate`.
- `metric.entity_id` is a **string**.
- `values` is `[[unix_ts, "string_value"], ...]`.
- Without `group_by`, series are merged across entities and carry no `entity_id` label,
  so always group by `entity_id`.
- dbaas returns `node_id` (e.g. `primary-5`) even when only `node_type` is grouped.
- Timestamps are aligned to request time, not to wall-clock buckets.
- Both edges can be partial. For nodebalancer (5 min buckets), the newest bucket
  (a few seconds old) read `0` while the previous one read 162 Bps. The check
  submits the newest point that is at least one bucket old.
- All entities in one request must be in the **same region**
  (`403 Entities belong to different data centers`).
- At most **5 metrics per request** (`400 Maximum limit of 5 metrics exceeded`).
- `group_by` accepts only `entity_id` and declared dimension labels
  (`node_id` is rejected even though it is returned). A dimension that one of the
  requested metrics lacks is accepted.
- Without `time_granularity`, points are returned at the scrape interval.

## Metric definitions (all `gauge`)

| service | metric | unit | aggregates | scrape | dimensions |
|---|---|---|---|---|---|
| dbaas | cpu_usage, read_iops, write_iops | %, IOPS | avg only | 60s | node_type |
| dbaas | memory_usage, disk_usage | % | avg/max/min/sum | 60s | node_type |
| dbaas | available_memory, available_disk | GB | avg/max/min/sum | 60s | node_type |
| nodebalancer | nb_{ingress,egress}[_tcp\|_udp]_traffic_rate | Bps | sum only | 300s | port, [protocol], config_id |
| nodebalancer | nb_new[_tcp\|_udp]_sessions_per_second | sessions/s | sum only | 300s | port, [protocol], config_id |
| nodebalancer | nb_total_active_sessions, nb_active_{tcp,udp}_sessions | Count | avg/min/max/sum | 300s | port, [protocol], config_id |
| nodebalancer | nb_total_active_backends, nb_active_{tcp,udp}_backends | Count | avg/min/max/sum | 300s | port, [protocol], config_id |

`protocol` is a dimension only on the non-TCP/UDP-specific metrics.
Each aggregate is validated per metric. Choose the aggregate from
`available_aggregate_functions`; do not hardcode it.

## Errors

| case | endpoint | status | body |
|---|---|---|---|
| unknown / inaccessible entity | token | 403 | `errors[].reason = "The following entity_ids are not valid - [...]"` |
| invalid / expired token | metrics | 401 | `"Invalid Token"` → re-issue the token and retry once |
| token for a different service_type | metrics | 403 | `"Token unauthorized for requested service type"` |
| entities from several regions | metrics | 403 | `"Entities belong to different data centers"` |
| unsupported aggregate | metrics | 400 | lists the supported functions |
| expired / revoked PAT | api.linode.com | 401 | → service check CRITICAL |

## Rate limit (metrics endpoint)

Headers: `X-RateLimit-Limit: 300`, `X-RateLimit-Remaining`, `Retry-After`.
The window length is not documented.

## Object Storage (`objectstorage`)

Observed 2026-09-16. Collected by the check since 0.2.0.

- The entity ID is the bucket hostname (for example `my-bucket.jp-tyo-1.linodeobjects.com`),
  not a number. List buckets with `GET /v4/object-storage/buckets`; the `hostname` field matches.
- **Token**: the body must be `{}`. Passing `entity_ids` returns
  `400 entity_ids are not supported for service type objectstorage`,
  even though the API reference documents them. The token covers every bucket on the account.
- **Metrics request**:
  - `entity_region` is required (`400 entity_region is required ...`).
  - `entity_ids` with bucket hostnames can be added to narrow the result.
  - `filters` on `entity_id` is rejected (`400 Invalid filter`).
- A region without Cloud Pulse support for Object Storage returns `200` with an empty result.
- Values can be `null` (for example the oldest hourly bucket).
- Dimensions: `endpoint` on every metric, plus `request_type` / `response_type` on
  `obj_requests_num`, `obj_requests_rps`, and `obj_responses_num`.
- Scrape intervals:
  - `obj_bucket_size`, `obj_bucket_num_objects`: 3600s. Use hourly granularity; the latest
    complete value is 1-2 hours old.
  - `obj_ttfb_average`: 900s.
  - Everything else: 60s.
- Request and response counts are per bucket per minute and can swing sharply
  between minutes (for example 1495 → 364 → 0 → 1487). Requests sent seconds apart
  can select different minutes, so two metrics from the same bucket may not line up.
- `obj_requests_num` grouped by `request_type` matches the per-type metrics
  (`obj_requests_get`, ...) for the same minute.

## LKE Enterprise (`lke`)

Checked 2026-09-24. Not verified against a live account.

- `GET /v4/monitor/services` returned only `dbaas`, `nodebalancer`, `objectstorage`
  and `logs`, and `GET /v4/monitor/services/lke/metric-definitions` returned
  `404 Not found`: Cloud Pulse metrics for LKE Enterprise are in limited
  availability and must be requested through a support ticket.
- The service type is `lke` (the name the Akamai OpenTelemetry collector uses).
- Entities are LKE cluster IDs from `GET /v4/lke/clusters`. Only clusters with
  `tier: enterprise` have metrics; standard clusters are skipped.
- Documented metrics (units from the docs): `lke_e_ready_worker_nodes` (count),
  `lke_e_not_ready_worker_nodes` (count), `lke_e_apiserver_request_rate` (request/s),
  `lke_e_apiserver_request_error_rate` (failed requests/s),
  `lke_e_apiserver_availability_percent` (%).
- Aggregations, scrape intervals and dimensions are read from
  `metric-definitions` at runtime, so they need no code change once access is granted.
