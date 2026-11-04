import { makeClientErrorHandler } from '@rask/api/observability';

// An unhandled browser error becomes one structured report with an id the person can quote.
export const handleError = makeClientErrorHandler('console');
