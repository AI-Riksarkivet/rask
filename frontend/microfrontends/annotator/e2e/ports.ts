/**
 * Every port this zone's e2e run binds — declared ONCE.
 *
 * The numbers used to live inline in playwright.config.ts, which was fine while there was one server
 * and one number. The transport migration added a mock upstream, and a port that drifts between the
 * config and the thing that binds it comes up where nobody is looking — `reuseExistingServer` is on locally, so playwright ADOPTS whatever already listens
 * instead of failing. `@rask/zone-contract` scans this file and asserts no two zones claim a port.
 */

/** The app server. ONE of them: there used to be a second with `MEDIA_ASSIST_URL` set, because
 *  runner presence was read from the web pod's own env and no browser can restub that. Presence now
 *  comes from the annotator SERVICE, which a spec seeds on the mock below — so the fact under test
 *  is seeded per-test instead of baked into a whole extra dev server. */
export const E2E_PORT = 5299;
/** The mock ANNOTATOR service standing in for the projects/tasks/assist upstreams, which the zone's
 *  remote functions now reach SERVER-side where `page.route` cannot see them. NOT a zone dev-server
 *  port. */
export const MOCK_ANNOTATOR_PORT = 5296;

export const E2E = `http://localhost:${E2E_PORT}`;
export const MOCK_ANNOTATOR = `http://localhost:${MOCK_ANNOTATOR_PORT}`;
