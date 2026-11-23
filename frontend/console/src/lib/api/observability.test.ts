/**
 * The client error report: one record per unhandled error, with nothing of the sign-in round trip in it.
 */
import { describe, expect, test } from 'vitest';
import { makeClientErrorHandler, type ErrorReport } from './observability';

/** The reports an unhandled error at `url` produces. */
function reportsAt(url: string): ErrorReport[] {
	const reports: ErrorReport[] = [];
	const handle = makeClientErrorHandler('console', (report) => reports.push(report));
	void handle({
		kind: 'unknown',
		error: new Error('boom'),
		event: { url: new URL(url), route: { id: null } },
		status: 500,
		message: 'Internal Error',
	} as unknown as Parameters<typeof handle>[0]);
	return reports;
}

describe('an unhandled error', () => {
	test.each([
		// The callback's query is the authorization code and its state.
		['http://console.test/auth/callback?code=the-code&state=the-state', '/auth/callback'],
		// A page's own query is how it is found again.
		['http://console.test/catalog/tables?project=acme', '/catalog/tables?project=acme'],
	])('is reported at %s as %s', (url, reported) => {
		expect(reportsAt(url).map((report) => report.url)).toEqual([reported]);
	});
});
