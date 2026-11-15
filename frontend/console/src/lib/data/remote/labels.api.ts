import { command, query } from '#lib/reads.svelte.ts';
import * as v from 'valibot';
import type { ApiResult } from '#lib/api/client.ts';
import { catalogJSON, catalogListing, parsed } from '#lib/doors.ts';
import {
	DefineLabelSchema,
	LABEL_SCOPES,
	LabelDefinitionSchema,
	LabelledObjectsSchema,
	ObjectLabelsSchema,
	type LabelDefinition,
	type LabelledObjects,
	type ObjectLabels,
} from '../labels';

// A project's labels on the catalog's label doors (ADR 0211). Reading a project's definitions or its
// labelled objects is a member's or the estate admin's (anyone else is answered 404); defining and
// deleting one is the project admin's; attaching and removing one is the object owner's (`can_label`).
// The catalog decides every one of those: the console offers the control and says the refusal.

const enc = encodeURIComponent;

const ObjectRef = { kind: v.picklist(LABEL_SCOPES), id: v.string() };

/** Every label `project` defines. */
export const fetchLabelDefinitions = query(
	v.string(),
	async (project): Promise<ApiResult<LabelDefinition[]>> =>
		parsed(
			await catalogJSON(`/management/v1/projects/${enc(project)}/labels`),
			v.array(LabelDefinitionSchema),
		),
);

/** Every object of `project` that carries a label the caller may see, each with its labels; every
 *  page of the listing is read. */
export const fetchLabelledObjects = query(
	v.string(),
	async (project): Promise<ApiResult<LabelledObjects>> =>
		parsed(
			await catalogListing(`/management/v1/projects/${enc(project)}/labels/objects`, 'objects'),
			LabelledObjectsSchema,
		),
);

/** Define a label, or evolve the definition its name holds (its scope and allowed values only grow,
 *  its kind never changes). */
export const defineLabel = command(
	({ project, label }) => `Define label ${label.name} in project ${project}`,
	v.object({ project: v.string(), label: DefineLabelSchema }),
	async ({ project, label }): Promise<ApiResult<LabelDefinition>> => {
		const result = parsed(
			await catalogJSON(`/management/v1/projects/${enc(project)}/labels/define`, {
				method: 'POST',
				body: JSON.stringify(label),
			}),
			LabelDefinitionSchema,
		);
		if (result.ok) void fetchLabelDefinitions(project).refresh();
		return result;
	},
);

/** Delete a label definition; the catalog refuses (409) while any object carries it. */
export const deleteLabel = command(
	({ project, name }) => `Delete label ${name} in project ${project}`,
	v.object({ project: v.string(), name: v.string() }),
	async ({ project, name }): Promise<ApiResult<LabelDefinition>> => {
		const result = parsed(
			await catalogJSON(`/management/v1/projects/${enc(project)}/labels/${enc(name)}/delete`, {
				method: 'POST',
			}),
			LabelDefinitionSchema,
		);
		if (result.ok) void fetchLabelDefinitions(project).refresh();
		return result;
	},
);

/** The labels one table or namespace carries, and the definitions that may be attached to it. */
export const fetchObjectLabels = query(
	v.object(ObjectRef),
	async ({ kind, id }): Promise<ApiResult<ObjectLabels>> =>
		parsed(
			await catalogJSON(`/management/v1/${kind}/${enc(id)}/labels/describe`, {
				method: 'POST',
				body: '{}',
			}),
			ObjectLabelsSchema,
		),
);

/** After a label write: the object's own labels, and, when the caller knows the object's project, the
 *  project's labelled objects the lists read. */
function refreshAfterLabelWrite(
	kind: ObjectLabels['kind'],
	id: string,
	project: string | undefined,
): void {
	void fetchObjectLabels({ kind, id }).refresh();
	if (project !== undefined) void fetchLabelledObjects(project).refresh();
}

/** Attach a label the object's project defines, or change the value it carries. A marker carries no
 *  value. `project`, when known, is the object's, which the write refreshes the labelled objects of. */
export const setObjectLabel = command(
	({ kind, id, name }) => `Label ${kind} ${id} with ${name}`,
	v.object({
		...ObjectRef,
		project: v.optional(v.string()),
		name: v.string(),
		value: v.optional(v.string()),
	}),
	async ({ kind, id, project, name, value }): Promise<ApiResult<ObjectLabels>> => {
		const result = parsed(
			await catalogJSON(`/management/v1/${kind}/${enc(id)}/labels/set`, {
				method: 'POST',
				body: JSON.stringify(value === undefined ? { name } : { name, value }),
			}),
			ObjectLabelsSchema,
		);
		if (result.ok) refreshAfterLabelWrite(kind, id, project);
		return result;
	},
);

/** Remove a label from the object (the catalog answers the same whether or not it carried it). */
export const removeObjectLabel = command(
	({ kind, id, name }) => `Remove label ${name} from ${kind} ${id}`,
	v.object({ ...ObjectRef, project: v.optional(v.string()), name: v.string() }),
	async ({ kind, id, project, name }): Promise<ApiResult<ObjectLabels>> => {
		const result = parsed(
			await catalogJSON(`/management/v1/${kind}/${enc(id)}/labels/remove`, {
				method: 'POST',
				body: JSON.stringify({ name }),
			}),
			ObjectLabelsSchema,
		);
		if (result.ok) refreshAfterLabelWrite(kind, id, project);
		return result;
	},
);
