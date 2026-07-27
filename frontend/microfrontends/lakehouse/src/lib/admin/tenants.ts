import * as v from 'valibot';

import type { components } from '@rask/api/generated/catalog';

// Wire contract for the catalog's first-class projects API (endpoints/projects.py): a BARE ARRAY of
// project summaries — parsed, not cast, at the browser boundary (the @rask/api parse-don't-validate rule).
// The static shapes come from the generated docs/catalog-openapi.json types (`bun run gen:types:catalog`)
// — never hand-mirrored — so a backend rename/removal stops compiling here instead of lying downstream.

export type ProjectWarehouse = components['schemas']['ProjectWarehouse'];
export type Project = components['schemas']['ProjectResponse'];

const ProjectWarehouseSchema: v.GenericSchema<unknown, ProjectWarehouse> = v.object({
	id: v.string(),
	bucket: v.string(),
	status: v.string(),
});

const ProjectSchema: v.GenericSchema<unknown, Project> = v.object({
	project: v.string(),
	warehouses: v.array(ProjectWarehouseSchema),
	admins: v.array(v.string()),
});

export const ProjectsResponseSchema = v.array(ProjectSchema);
