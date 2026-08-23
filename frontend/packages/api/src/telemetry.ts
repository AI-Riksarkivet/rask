// @rask/api/telemetry — the SSR zones' server spans.
//
// THE GAP THIS CLOSES. A browser reaches a zone's SvelteKit/Bun server BEFORE anything reaches the
// gateway, and the zones exported nothing at all. So a slow page with a healthy gateway had no span
// anywhere to explain it, and the RED dashboard's unfiltered `sum by (service_name)` rendered the
// zones' absence as "these services do not exist" rather than "these services are unmonitored".
//
// MANUAL INSTRUMENTATION ONLY, and that is a Bun constraint rather than a preference. The
// `@opentelemetry/auto-instrumentations-*` packages hook module loading through `require-in-the-middle`
// / `import-in-the-middle`, which need Node's loader hooks; Bun implements those incompletely. What was
// verified before any of this was written, on the exact `oven/bun:1-debian` digest the zone image runs:
// spans are created, they parent correctly, and AsyncLocalStorage SURVIVES AN AWAIT — which is the one
// property a per-request server span actually depends on.
//
// Complementary to `./observability`, not a replacement: that module attributes ERRORS to a zone; this
// one gives every request a span. Its own comment anticipated this ("point `sink` at an OTLP
// exporter"), and the two share the zone name so a failing request and its span agree on who owns it.
import { context, SpanKind, SpanStatusCode, trace } from '@opentelemetry/api';
import { OTLPTraceExporter } from '@opentelemetry/exporter-trace-otlp-http';
import { resourceFromAttributes } from '@opentelemetry/resources';
import { BatchSpanProcessor } from '@opentelemetry/sdk-trace-base';
import { NodeTracerProvider } from '@opentelemetry/sdk-trace-node';
import type { Handle } from '@sveltejs/kit';

/** The subset of the zone's env this module reads. Absent endpoint = telemetry off, which is the default. */
export interface TelemetryEnv {
	OTEL_EXPORTER_OTLP_ENDPOINT?: string | undefined;
	OTEL_SERVICE_NAME?: string | undefined;
	OTEL_EXPORTER_OTLP_HEADERS?: string | undefined;
	OTEL_EXPORTER_OTLP_TRACES_HEADERS?: string | undefined;
}

const TRACER_NAME = 'rask.zone';

/** `k=v,k=v` — the OTLP header env format. Returns `{}` for absent or malformed input rather than throwing. */
function parseHeaders(raw: string | undefined): Record<string, string> {
	if (!raw) return {};
	const out: Record<string, string> = {};
	for (const pair of raw.split(',')) {
		const eq = pair.indexOf('=');
		if (eq > 0) out[pair.slice(0, eq).trim()] = pair.slice(eq + 1).trim();
	}
	return out;
}

let started = false;

/**
 * Register a tracer provider for this zone, once per process.
 *
 * A no-op when `OTEL_EXPORTER_OTLP_ENDPOINT` is unset, which is what keeps `make dev-zone` and the
 * per-zone Playwright suites working with no collector behind them — the same opt-in shape the Python
 * seam uses, so "off" is selectable rather than merely unconfigured.
 */
export function startZoneTelemetry(env: TelemetryEnv): boolean {
	if (started) return true;
	const endpoint = env.OTEL_EXPORTER_OTLP_ENDPOINT;
	if (!endpoint) return false;

	// Traces get the signal-specific headers when present. GreptimeDB requires a pipeline header on
	// traces that metrics must NOT carry, which is why the two are separate variables upstream; through
	// a Collector neither is set and the Collector adds what its backend needs.
	const headers = {
		...parseHeaders(env.OTEL_EXPORTER_OTLP_HEADERS),
		...parseHeaders(env.OTEL_EXPORTER_OTLP_TRACES_HEADERS),
	};

	const provider = new NodeTracerProvider({
		resource: resourceFromAttributes({ 'service.name': env.OTEL_SERVICE_NAME ?? 'rask-zone' }),
		// The exporter appends `/v1/traces` itself, matching the Python seam's `otlphttp` behaviour.
		spanProcessors: [
			new BatchSpanProcessor(
				new OTLPTraceExporter({ url: `${endpoint.replace(/\/$/, '')}/v1/traces`, headers }),
			),
		],
	});
	provider.register();
	started = true;
	return true;
}

/**
 * One SERVER span per request, named by ROUTE ID rather than URL.
 *
 * `route.id` is `/table/[id]`, not `/table/abc123`. A span name is a metric dimension in waiting —
 * every span-metrics connector promotes it — so a concrete URL here would mint one series per object
 * id, which is the rule this estate has already been burned by breaking at the sidecar.
 */
export function makeTelemetryHandle(zone: string): Handle {
	return async ({ event, resolve }) => {
		const tracer = trace.getTracer(TRACER_NAME);
		const routeId = event.route.id ?? 'unmatched';
		return tracer.startActiveSpan(
			`${event.request.method} ${routeId}`,
			{
				kind: SpanKind.SERVER,
				attributes: {
					'http.request.method': event.request.method,
					'http.route': routeId,
					'url.path': event.url.pathname,
					'lance.zone': zone,
				},
			},
			context.active(),
			async (span) => {
				try {
					const response = await resolve(event);
					span.setAttribute('http.response.status_code', response.status);
					// Only a FINAL failure is an error. A 404 is a correct answer to a wrong URL, so it stays
					// UNSET — marking it would make every crawler a red span.
					if (response.status >= 500)
						span.setStatus({ code: SpanStatusCode.ERROR, message: `HTTP ${response.status}` });
					return response;
				} catch (error) {
					// The status message carries the error CLASS, never the stack — stacks belong on a log
					// record, which `./observability` already emits for this same request.
					span.setStatus({
						code: SpanStatusCode.ERROR,
						message: error instanceof Error ? `${error.name}: ${error.message}` : 'unknown error',
					});
					throw error;
				} finally {
					span.end();
				}
			},
		);
	};
}
