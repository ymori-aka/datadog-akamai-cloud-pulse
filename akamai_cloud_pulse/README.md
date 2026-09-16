# Agent Check: Akamai Cloud Pulse

> **Unofficial.** This check is a community project. It is not published by Datadog
> and is not supported by Akamai Technologies or Datadog, Inc.

## Overview

[Akamai Cloud Pulse][1] is the monitoring service for Akamai Cloud (formerly Linode).
It exposes metrics for managed services that you cannot install an Agent on.

This check polls the Cloud Pulse API and submits the metrics to Datadog, so you can
chart and alert on them next to the rest of your stack.

Supported services:

| Cloud Pulse `service_type` | Product | Metrics |
|---|---|---|
| `dbaas` | Managed Databases | CPU, memory, disk, IOPS per node |
| `nodebalancer` | NodeBalancers | traffic rate, sessions, new sessions, active backends per port |
| `objectstorage` | Object Storage | content stored, objects, requests, responses, throughput, time to first byte per bucket |

The check tags every metric with the entity ID, label, and region. It adds the
database engine and version for databases, and the LKE cluster for NodeBalancers
that LKE creates. Object Storage metrics are tagged with `bucket` and `endpoint`,
and with `request_type` or `response_type` where Cloud Pulse provides them.

## Setup

### Prerequisites

Create an Akamai Cloud [personal access token][2] with these scopes:

- `Monitor`: Read Only
- `Databases`: Read Only (for `dbaas`)
- `NodeBalancers`: Read Only (for `nodebalancer`)
- `Object Storage`: Read Only (for `objectstorage`)

Access to the Cloud Pulse API may require enrollment, depending on your account.

### Installation

This check is not yet included in the Datadog Agent or published as a Datadog
community integration. Install it from a wheel on a host running Agent v7:

1. Download `datadog_akamai_cloud_pulse-<VERSION>-py3-none-any.whl` from the
   [GitHub releases][3], or build it with `ddev release build akamai_cloud_pulse`.
2. Install it:

   ```shell
   sudo -u dd-agent datadog-agent integration install -w /path/to/datadog_akamai_cloud_pulse-<VERSION>-py3-none-any.whl
   ```

In containers, bake the wheel into a custom Agent image. See [`deploy/`][4].

### Configuration

1. Create `akamai_cloud_pulse.d/conf.yaml` in the `conf.d/` folder of your
   [Agent's configuration directory][5]. See the [sample conf.yaml][6] for all options.

   ```yaml
   init_config:

   instances:
     - personal_access_token: <PERSONAL_ACCESS_TOKEN>
       min_collection_interval: 60
       empty_default_hostname: true
       services:
         - service_type: dbaas
         - service_type: nodebalancer
           entity_ids: [1234567]
         - service_type: objectstorage
           entity_regions: [us-east]
   ```

   - If you omit `entity_ids`, the check discovers every entity the token can see
     and refreshes the list hourly.
   - If you omit `metrics`, the check collects every metric in the service's metric definitions.
   - For `objectstorage`, `entity_ids` are bucket hostnames
     (for example `my-bucket.us-east-1.linodeobjects.com`), and `entity_regions`
     limits the regions collected. Without them, every region that has a bucket is
     queried. Regions where Cloud Pulse does not support Object Storage return no data.
   - Keep the token out of plain-text config with [secrets management][7], for
     example `personal_access_token: ENC[cloud_pulse_pat]`.

2. Run the check on **one** Agent only. On Kubernetes, use a [cluster check][8].
   Cloud Pulse metrics describe managed services, so running the check on every
   node sends duplicate data.

3. [Restart the Agent][9].

### Validation

[Run the Agent's status subcommand][10] and look for `akamai_cloud_pulse` under the Checks section.

## How the data is collected

- For each service, the check requests a short-lived Cloud Pulse token, which it
  caches for 5 hours and renews on `401`. It then queries the metrics endpoint
  with at most 5 metrics per request, as the API requires.
- Each metric uses the aggregate function the API allows for it:
  `avg` where available, otherwise `sum`, `max`, or `min`.
  The function used is reported in the `aggregate_function` tag.
- Entities are queried one region at a time, because Cloud Pulse rejects
  requests that mix regions.
- Object Storage metrics are queried per region with `entity_region`. Cloud Pulse
  does not accept bucket IDs on the token request, so the token covers every
  bucket on the account.
- Cloud Pulse aggregates data into buckets that match each metric's scrape
  interval: 1 minute for databases and most Object Storage metrics, 5 minutes for
  NodeBalancers, 15 minutes for Object Storage time to first byte, and 1 hour for
  Object Storage content stored and object count.
- The newest bucket is often incomplete, so the check submits only the latest
  **complete** bucket. Values therefore trail real time by one bucket.
- The Agent timestamps each value when it submits it, not with the bucket time.
- The check queries each group of metrics at most once per bucket, even when
  `min_collection_interval` is shorter. Hourly metrics therefore get one data
  point per hour.
- Object Storage request and response metrics exist only when the bucket was
  used during the interval. Gaps mean no activity, not a collection failure.

## Data Collected

### Metrics

See [metadata.csv][11] for a list of metrics provided by this integration.

Metrics collected through this check count toward your
[custom metrics][12] usage.

### Service Checks

`akamai_cloud_pulse.can_connect`, tagged by `service_type`:
- `CRITICAL` if the token is rejected or the API cannot be reached.
- `OK` otherwise.

### Events

This integration does not include any events.

### Dashboards and monitors

- `assets/dashboards/akamai_cloud_pulse_overview.json`: overview dashboard.
  Create a blank dashboard, then use **Configure > Import dashboard JSON** to load it.
- `assets/monitors/`: monitor templates for API failures, high database disk
  usage, NodeBalancer ports with no healthy backends, and Object Storage buckets
  returning 5xx errors.

## Troubleshooting

| Symptom | Cause |
|---|---|
| `can_connect` is `CRITICAL` with `Unauthorized` | The token expired, was revoked, or lacks the `Monitor` scope. |
| `403` with `entity_ids are not valid` | A configured entity was deleted or the token cannot read it. |
| No Object Storage metrics for a region | Cloud Pulse does not support Object Storage metrics in that region yet. |
| Unauthorized when listing buckets | The token lacks the `Object Storage` read scope. |
| A metric is missing for some entities | The metric does not apply, for example UDP metrics on a NodeBalancer with only TCP configs. |

Open an issue at [ymori-aka/datadog-akamai-cloud-pulse][13].

[1]: https://techdocs.akamai.com/cloud-computing/docs/akamai-cloud-pulse
[2]: https://techdocs.akamai.com/cloud-computing/docs/manage-personal-access-tokens
[3]: https://github.com/ymori-aka/datadog-akamai-cloud-pulse/releases
[4]: https://github.com/ymori-aka/datadog-akamai-cloud-pulse/tree/main/deploy
[5]: https://docs.datadoghq.com/agent/guide/agent-configuration-files/#agent-configuration-directory
[6]: https://github.com/ymori-aka/datadog-akamai-cloud-pulse/blob/main/akamai_cloud_pulse/datadog_checks/akamai_cloud_pulse/data/conf.yaml.example
[7]: https://docs.datadoghq.com/agent/configuration/secrets-management/
[8]: https://docs.datadoghq.com/containers/cluster_agent/clusterchecks/
[9]: https://docs.datadoghq.com/agent/guide/agent-commands/#start-stop-and-restart-the-agent
[10]: https://docs.datadoghq.com/agent/guide/agent-commands/#agent-status-and-information
[11]: https://github.com/ymori-aka/datadog-akamai-cloud-pulse/blob/main/akamai_cloud_pulse/metadata.csv
[12]: https://docs.datadoghq.com/metrics/custom_metrics/
[13]: https://github.com/ymori-aka/datadog-akamai-cloud-pulse/issues
