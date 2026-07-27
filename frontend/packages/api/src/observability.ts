// @rask/api/observability — per-zone error attribution.
//
// A page composed from independently deployed zones fails PER ZONE, and a generic error report cannot
// say WHICH zone broke: every zone serves from the same origin, so "the page 500'd" is all a plain
// report carries. The micro-frontends principle is to attribute observability the same way everything
// else is attributed — by the owning zone — so an alert names the zone that can fix it.
//
// This is the smallest thing that does that honestly: a `zone` dimension stamped onto every client and
// server error, emitted through a sink the deployment chooses. The default sink is `console`, which is
// what the zones' Bun servers already ship to the cluster's log pipeline; point `sink` at an OTLP
// exporter or an error tracker without touching a single zone.
import type { HandleClientError, HandleServerError } from '@sveltejs/kit';

/** The attributed error record. Flat and JSON-serialisable on purpose — it is a log line. */
export interface ZoneErrorReport {
	/** The owning zone — the whole point of this module. */
	zone: string;
	/** 'client' = the browser bundle, 'server' = the zone's SSR/Bun server. */
	side: 'client' | 'server';
	message: string;
	/** SvelteKit's per-occurrence id, so a user-visible page and a log line can be tied together. */
	eventId: string;
	/** The route id that failed (`/table/[id]`), not the concrete URL — safe to group alerts on. */
	routeId: string | null;
	url: string;
	status?: number | undefined;
	stack?: string | undefined;
}

export type ZoneErrorSink = (report: ZoneErrorReport) => void;

/** Default sink: one structured line on stderr. The zones' Bun servers and the browser console both
 *  carry it, and the cluster log pipeline already scrapes the former. */
export const consoleSink: ZoneErrorSink = (report) => {
	console.error(`[zone-error] ${JSON.stringify(report)}`);
};

/** The message shown to the user. Never the raw error: an unhandled error's message can carry an
 *  upstream URL, a token fragment or a stack path. The eventId is what support asks for. */
const userMessage = (eventId: string) => `Something went wrong. Reference: ${eventId}`;

function toReport(
	zone: string,
	side: 'client' | 'server',
	error: unknown,
	event: { url: URL; route: { id: string | null } },
	status: number | undefined,
	eventId: string,
): ZoneErrorReport {
	const err = error instanceof Error ? error : undefined;
	return {
		zone,
		side,
		message: err?.message ?? String(error),
		eventId,
		routeId: event.route.id,
		url: event.url.pathname + event.url.search,
		status,
		stack: err?.stack,
	};
}

/**
 * `hooks.client.ts` — attribute an unhandled client-side error to this zone.
 *
 * This is also the only party that can report a zone's own hydration/navigation failure: nothing on the
 * server sees it.
 */
export function makeZoneClientErrorHandler(
	zone: string,
	sink: ZoneErrorSink = consoleSink,
): HandleClientError {
	return ({ error, event, status, message }) => {
		// SvelteKit passes a stable per-occurrence id in `event`-adjacent metadata on the server only, so
		// on the client we mint one; it still ties the visible page to the logged line.
		const eventId = crypto.randomUUID();
		sink(toReport(zone, 'client', error, event, status, eventId));
		// 404s are routing, not faults — keep SvelteKit's own message so the page still reads correctly.
		return { message: status === 404 ? message : userMessage(eventId) };
	};
}

/** `hooks.server.ts` — attribute an unhandled SSR/endpoint error to this zone. */
export function makeZoneServerErrorHandler(
	zone: string,
	sink: ZoneErrorSink = consoleSink,
): HandleServerError {
	return ({ error, event, status, message }) => {
		const eventId = crypto.randomUUID();
		sink(toReport(zone, 'server', error, event, status, eventId));
		return { message: status === 404 ? message : userMessage(eventId) };
	};
}
