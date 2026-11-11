import { makeClientErrorHandler } from '#lib/api/observability.ts';

// An unhandled browser error becomes one structured report with an id the person can quote.
export const handleError = makeClientErrorHandler('console');
