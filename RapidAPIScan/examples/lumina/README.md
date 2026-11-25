# Example: Lumina BI chatbot

An AI business-intelligence chatbot, used here as an example of monitoring a
real multi-tier application with RapidPulse:

Web App (Windows EC2) → Lumina API → MCP gateway → Data Fabric API → Cube API → SQL Server (Windows EC2),
with AWS Bedrock LLM calls from Lumina and Fabric.

| File | Purpose |
|---|---|
| [`topology.yaml`](topology.yaml) | Simulator description of the application: `.\scripts\run-demo.ps1 -Example lumina` |
| [`pulse.yaml`](pulse.yaml) | RapidPulse config for it: display names, per-service latency targets, `attribute_aliases` for Lumina's own `lumina.*` LLM attributes |
| [`INTEGRATION.md`](INTEGRATION.md) | Code-level changes to instrument the real services, written from a scan of `medm-lumina-api` |
| [`otel-collector.yaml`](otel-collector.yaml) | Collector config that sends Lumina's telemetry to both Phoenix and RapidPulse |

Nothing in this folder is needed by RapidPulse itself. Delete it, and RapidPulse still
monitors Lumina from the telemetry alone. Without the `pulse.yaml` settings,
you only lose the display names, latency targets and Lumina-computed costs.
