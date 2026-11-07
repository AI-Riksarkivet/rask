/**
 * Every port the console's e2e run binds — declared ONCE, imported by the Playwright config, the mocks
 * and the specs. `reuseExistingServer` is on locally, so a duplicate does not fail with EADDRINUSE: it
 * silently ADOPTS whatever is already listening, and a suite would drive a real server as its "mock".
 *
 * Three origins of the one build, one per family of specs: auth-OFF (a config of `{"signIn": false}`),
 * auth-ON for the estate pages (projects, settings, the activity log) and auth-ON for the lakehouse's
 * pages (`admin/`), each with the mock upstreams its specs seed (`admin/ports.ts` has the latter's).
 */

/** The auth-OFF preview: the anonymous landing and the lakehouse's auth-off pages. */
export const AUTH_OFF_PORT = 5293;
/** The auth-ON preview for the estate pages (`projects/`, `settings/`, `activity/`). */
export const AUTH_ON_PORT = 5288;
/** The mock CATALOG behind the estate pages' `/api/catalog`, seeded per bearer. */
export const MOCK_CATALOG_PORT = 5287;
/** The mock LINEAGE service behind `/api/lineage` (the live cursor, the DLQ). */
export const MOCK_LINEAGE_PORT = 5291;
/** The mock OIDC issuer, served same-origin under `/oidc` by every auth-ON preview. */
export const MOCK_OIDC_PORT = 5284;

export const AUTH_OFF = `http://localhost:${AUTH_OFF_PORT}`;
export const AUTH_ON = `http://localhost:${AUTH_ON_PORT}`;
export const MOCK_CATALOG = `http://localhost:${MOCK_CATALOG_PORT}`;
export const MOCK_LINEAGE = `http://localhost:${MOCK_LINEAGE_PORT}`;
