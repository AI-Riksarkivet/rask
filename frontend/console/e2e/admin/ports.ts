/**
 * The ports of the lakehouse pages' auth-ON preview and its mocks (see `../ports.ts` for the rule that
 * every port is declared once).
 */
export { MOCK_LINEAGE } from '../ports';

/** The auth-ON preview for the lakehouse's own pages (`/catalog/*`, `/admin/*`). */
export const AUTH_ON_PORT = 5295;
/** The mock catalog behind this preview's `/api/catalog`, seeded per bearer. */
export const MOCK_CATALOG_PORT = 5292;

export const AUTH_ON = `http://localhost:${AUTH_ON_PORT}`;
export const MOCK_CATALOG = `http://localhost:${MOCK_CATALOG_PORT}`;
