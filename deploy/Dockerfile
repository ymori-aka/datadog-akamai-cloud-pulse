# Datadog Agent with the Akamai Cloud Pulse check preinstalled.
# Build from the repository root after `ddev release build akamai_cloud_pulse`:
#   docker build -f deploy/Dockerfile -t <IMAGE> .
ARG AGENT_VERSION=7
FROM registry.datadoghq.com/agent:${AGENT_VERSION}

COPY akamai_cloud_pulse/dist/datadog_akamai_cloud_pulse-*-py3-none-any.whl /tmp/
RUN agent integration install -r -w /tmp/datadog_akamai_cloud_pulse-*-py3-none-any.whl \
    && rm -f /tmp/datadog_akamai_cloud_pulse-*.whl
