// Next.js instrumentation hook (place in src/instrumentation.ts of any Next.js app / BFF).
// Starts a server span per request and propagates W3C `traceparent` on every
// outbound fetch — so the call to your backend API continues the same trace.
//
//   npm i @opentelemetry/sdk-node@0.57.2 @opentelemetry/exporter-trace-otlp-http@0.57.2 \
//         @opentelemetry/auto-instrumentations-node@0.56.0 @opentelemetry/resources@1.30.1
//   env: OTEL_EXPORTER_OTLP_ENDPOINT=http://<pulse>:4400   OTEL_EXPORTER_OTLP_HEADERS=x-pulse-key=<key>
export async function register() {
  if (process.env.NEXT_RUNTIME !== "nodejs") return;
  const { NodeSDK } = await import("@opentelemetry/sdk-node");
  const { OTLPTraceExporter } = await import("@opentelemetry/exporter-trace-otlp-http");
  const { getNodeAutoInstrumentations } = await import("@opentelemetry/auto-instrumentations-node");
  const { Resource } = await import("@opentelemetry/resources");
  const os = await import("node:os");

  const sdk = new NodeSDK({
    resource: new Resource({
      "service.name": process.env.OTEL_SERVICE_NAME ?? "web-app",
      "service.namespace": process.env.PULSE_APP ?? "default",
      "deployment.environment.name": process.env.DEPLOY_ENV ?? "",
      "host.name": os.hostname(),
    }),
    traceExporter: new OTLPTraceExporter(),          // reads OTEL_EXPORTER_OTLP_ENDPOINT / _HEADERS
    instrumentations: [getNodeAutoInstrumentations({
      "@opentelemetry/instrumentation-fs": { enabled: false },
      "@opentelemetry/instrumentation-http": {
        ignoreIncomingRequestHook: (req) => /^\/(_next|favicon|health)/.test(req.url ?? ""),
        // Never record cookies / auth headers; the URL query string is stripped by RapidPulse too.
        headersToSpanAttributes: { client: { requestHeaders: [] }, server: { requestHeaders: [] } },
      },
    })],
  });
  sdk.start();
}
