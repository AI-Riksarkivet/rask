// The catalog wire CONTRACTS the projects surface reads — types only.
//
// The `*.api.ts` modules hold the reads and writes; their types live in a sibling
// non-remote module (same split as the console's `data/catalog.ts` ↔
// `data/remote/warehouses.api.ts`). Generated from the catalog's OpenAPI, never hand-mirrored.
//
// A project is the TOP of the hierarchy (project › warehouse › namespace › table), so the estate's
// project list and one project's overview are estate-level pages (`/projects`); the catalog pages keep
// everything BELOW a project.
import type { components } from '@rask/api/generated/catalog';

type Warehouse = components['schemas']['WarehouseResponse'];
type CreateWarehouse = components['schemas']['CreateWarehouseRequest'];

/** `WarehouseResponse` + the additive `serving` class the registry stamps (work vs gold). */
export type WarehouseRecord = Warehouse & { serving?: string | null };

/** What the create dialog posts: the generated request narrowed to the serving classes we mint. */
export type CreateWarehouseBody = CreateWarehouse & { serving?: 'gold' };

/** `ProjectResponse` — one tenant's warehouses (a narrower record than the registry's) + its
 *  effective admins (`[]` when FGA is off/unavailable — degraded, never fabricated). */
export type ProjectSummary = components['schemas']['ProjectResponse'];
