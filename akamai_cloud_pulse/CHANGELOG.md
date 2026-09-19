# CHANGELOG - akamai_cloud_pulse

<!-- towncrier release notes start -->

## 0.2.0 / 2026-09-19

***Added***:

* Collect Object Storage (`objectstorage`) metrics. Bucket hostnames can be used in `entity_ids`, and the new `entity_regions` option limits the regions collected.

***Fixed***:

* Query each group of metrics at most once per scrape interval, so hourly metrics are no longer fetched and submitted every run.
* Skip null data points, and log a warning when Cloud Pulse rejects the credentials.

## 0.1.0 / 2026-09-16

***Added***:

* Initial Release: collect Managed Database (`dbaas`) and NodeBalancer metrics from Akamai Cloud Pulse, with an overview dashboard and monitor templates.
