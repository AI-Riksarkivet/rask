import { makeZoneClientErrorHandler } from '@rask/api/observability';

// The browser half of per-zone error attribution. This is also the ONLY party that can report this
// zone's own hydration or client-navigation failure — the server never sees one.
export const handleError = makeZoneClientErrorHandler('annotator');
