/**
 * Every port this zone's e2e run binds — declared ONCE.
 *
 * `@rask/zone-contract` scans this file (`manifest.test.ts`, "no two zones bind the same port, in dev
 * OR in e2e") and asserts no two zones claim a port. That gate exists because playwright runs with
 * `reuseExistingServer` locally: a duplicate does not fail loudly with EADDRINUSE, it silently ADOPTS
 * whatever is already listening — which is how the lakehouse admin suite once pointed its "mock
 * catalog" at the media zone's real dev server.
 *
 * 5289–5299 are claimed by the lakehouse, media and annotator suites, so this zone's two new servers
 * take the next free pair BELOW that block.
 */

/** The auth-OFF dev server: the OIDC fail-safe routes + the anonymous landing (`e2e/*.spec.ts`). */
export const AUTH_OFF_PORT = 5293;
/**
 * The auth-ON dev server: the PROJECTS surface (`e2e/projects/*.spec.ts`), which only exists for a
 * signed-in caller — the estate-admin create flow, the membership gallery and one project's overview
 * all key off `/v1/me`, and `/v1/me` needs a bearer. One zone cannot be auth-ON and auth-OFF at once,
 * so this is a second dev server, exactly as the lakehouse zone runs one for its admin area.
 */
export const AUTH_ON_PORT = 5288;
/** The mock CATALOG standing in for every `/v1/*` read and write the projects surface makes
 *  SERVER-side, where `page.route` cannot reach. NOT a zone dev-server port. */
export const MOCK_CATALOG_PORT = 5287;

export const AUTH_OFF = `http://localhost:${AUTH_OFF_PORT}`;
export const AUTH_ON = `http://localhost:${AUTH_ON_PORT}`;
export const MOCK_CATALOG = `http://localhost:${MOCK_CATALOG_PORT}`;
