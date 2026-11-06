// @rask/api/observability — the console's client error report.
//
// An unhandled browser error becomes one structured record with an id the person can quote, emitted
// through a sink the app chooses (the browser console by default), and the person sees the id, never
// the raw error.
import type { HandleClientError } from '@sveltejs/kit/hooks';

/** The attributed error record. Flat and JSON-serialisable on purpose — it is a log line. */
export interface ErrorReport {
	/** The app that failed. */
	app: string;
	message: string;
	/** SvelteKit's per-occurrence id, so a user-visible page and a log line can be tied together. */
	eventId: string;
	/** The route id that failed (`/table/[id]`), not the concrete URL — safe to group alerts on. */
	routeId: string | null;
	url: string;
	status?: number | undefined;
	stack?: string | undefined;
}

export type ErrorSink = (report: ErrorReport) => void;

/** Default sink: one structured line on the browser console. */
export const consoleSink: ErrorSink = (report) => {
	console.error(`[console-error] ${JSON.stringify(report)}`);
};

/** The message shown to the user. Never the raw error: an unhandled error's message can carry an
 *  upstream URL, a token fragment or a stack path. The eventId is what support asks for. */
const userMessage = (eventId: string) => `Something went wrong. Reference: ${eventId}`;

function toReport(
	app: string,
	error: unknown,
	event: { url: URL; route: { id: string | null } },
	status: number | undefined,
	eventId: string,
): ErrorReport {
	const err = error instanceof Error ? error : undefined;
	return {
		app,
		message: err?.message ?? String(error),
		eventId,
		routeId: event.route.id,
		url: event.url.pathname + event.url.search,
		status,
		stack: err?.stack,
	};
}

/** `hooks.client.ts` — report an unhandled client-side error, with an id the person can quote.
 *  An app error (`error(status, message)`) is the app's own safe message and passes through unreported;
 *  a framework error (a 404) keeps SvelteKit's safe message, so the page still reads correctly. */
export function makeClientErrorHandler(
	app: string,
	sink: ErrorSink = consoleSink,
): HandleClientError {
	return (caught) => {
		if (caught.kind === 'app') return caught.error;
		const eventId = crypto.randomUUID();
		if (caught.kind === 'framework') {
			sink(toReport(app, caught.error.message, caught.event, caught.error.status, eventId));
			return caught.error;
		}
		// An unknown error renders as 500, SvelteKit's default for it.
		sink(toReport(app, caught.error, caught.event, 500, eventId));
		return { message: userMessage(eventId) };
	};
}
