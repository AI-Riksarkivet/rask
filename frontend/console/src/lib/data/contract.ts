/**
 * A TABLE'S DATA CONTRACT (ADR 0212): the ODCS v3 document its owner set at the catalog's
 * `contract/set` door, naming the columns the table's consumers rely on. The catalog holds every commit
 * to the table to it. The console reads it through `contract/describe` and shows it; it writes none.
 *
 * The response envelope mirrors the catalog's generated contract (`#lib/api/mirrors.ts`). The
 * document inside it is ODCS, which the generated type leaves open (`{[key: string]: unknown}`): the
 * schema below reads the fields the console shows and drops the rest, and the catalog validated the
 * whole document when it was set.
 */
import * as v from 'valibot';
import type { components } from '#lib/api/generated/catalog.ts';
import { mirrors } from '#lib/api/mirrors.ts';

type ContractWire = components['schemas']['ContractResponse'];

/** One ODCS property: a column, or a field nested in one. */
export type ContractProperty = {
	name: string;
	physicalType?: string | undefined;
	required?: boolean | undefined;
	primaryKey?: boolean | undefined;
	properties?: ContractProperty[] | undefined;
};

const PropertySchema: v.GenericSchema<ContractProperty> = v.object({
	name: v.string(),
	physicalType: v.optional(v.string()),
	required: v.optional(v.boolean()),
	primaryKey: v.optional(v.boolean()),
	properties: v.optional(v.array(v.lazy(() => PropertySchema))),
});

const SchemaObjectSchema = v.object({
	name: v.string(),
	properties: v.optional(v.array(PropertySchema)),
});

export const ContractDocumentSchema = v.object({
	apiVersion: v.string(),
	kind: v.string(),
	id: v.string(),
	version: v.string(),
	status: v.string(),
	name: v.optional(v.string()),
	schema: v.optional(v.array(SchemaObjectSchema)),
});
export type ContractDocument = v.InferOutput<typeof ContractDocumentSchema>;

export const ContractResponseSchema = mirrors<ContractWire, 'contract'>()(
	v.object({
		table: v.string(),
		uuid: v.string(),
		// The document is ODCS, which the model leaves open; the console reads the fields it shows.
		contract: v.optional(v.nullable(ContractDocumentSchema)),
		set_by: v.optional(v.nullable(v.string())),
		set_at: v.optional(v.nullable(v.string())),
	}),
);
export type ContractResponse = v.InferOutput<typeof ContractResponseSchema>;

/** One column the contract names, flattened: a nested field is named by its dotted path. */
export type ContractColumn = {
	path: string;
	physicalType: string | null;
	required: boolean;
	primaryKey: boolean;
};

/** Every property of the document's schema objects, depth first, each nested one under its parent's
 *  path, so `address.city` reads as Lance names a nested field. */
export function contractColumns(document: ContractDocument): ContractColumn[] {
	const out: ContractColumn[] = [];
	const walk = (properties: readonly ContractProperty[], prefix: string): void => {
		for (const property of properties) {
			const path = prefix ? `${prefix}.${property.name}` : property.name;
			out.push({
				path,
				physicalType: property.physicalType ?? null,
				required: property.required === true,
				primaryKey: property.primaryKey === true,
			});
			if (property.properties) walk(property.properties, path);
		}
	};
	for (const object of document.schema ?? []) walk(object.properties ?? [], '');
	return out;
}
