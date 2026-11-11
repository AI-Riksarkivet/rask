import * as v from 'valibot';

import type { components } from '#lib/api/generated/catalog.ts';

// The control-plane change-event wire contract (services/catalog endpoints/events.py) — parsed (not cast)
// at the wire boundary so a drift from the backend throws here rather than lying downstream (the `#lib/api`
// parse-don't-validate rule). The shapes are pinned to the generated docs/catalog-openapi.json types
// (`bun run gen:types:catalog`) — never hand-mirrored: `satisfies` keeps each schema's keys in lockstep
// with the spec, so a renamed/removed/added wire field stops compiling here. `action`/`object_type`
// deliberately stay open strings at runtime — a NEW backend action must render in the feed, not wedge it.

type EventWire = components['schemas']['CatalogControlEvent'];
type PageWire = components['schemas']['EventsResponse'];

export const ControlEventSchema = v.object({
	event_id: v.string(),
	occurred_at: v.string(), // ISO-8601 UTC
	action: v.string(),
	object_type: v.string(),
	object_id: v.string(),
	actor: v.nullable(v.string()),
	extra: v.record(v.string(), v.unknown()),
	namespace_uuid: v.optional(v.nullable(v.string())),
	object_name: v.optional(v.nullable(v.string())),
	object_uuid: v.optional(v.nullable(v.string())),
} satisfies Record<keyof EventWire, unknown>);
export type ControlEvent = v.InferOutput<typeof ControlEventSchema>;

export const EventsPageSchema = v.object({
	events: v.array(ControlEventSchema),
	cursor: v.number(),
	reset: v.boolean(),
} satisfies Record<keyof PageWire, unknown>);

/** What a grant event's `extra` names: the relation and the subject it was granted to or revoked
 *  from, and the condition a time-boxed grant carries. The catalog's grant doors write these keys;
 *  `extra` is open per action, so it is parsed here, where a grant is read out of it. */
const GrantExtraSchema = v.object({
	relation: v.string(),
	subject: v.string(),
	condition: v.optional(v.nullable(v.string()), null),
});
type GrantDetail = v.InferOutput<typeof GrantExtraSchema>;

/** The grant an event announces, or `null` for an event whose `extra` names none. */
export function grantDetail(e: ControlEvent): GrantDetail | null {
	const parsed = v.safeParse(GrantExtraSchema, e.extra);
	return parsed.success ? parsed.output : null;
}

/** The object an event is about, by the name the catalog gave it; the id only when it named none. */
export const objectLabel = (e: ControlEvent): string => e.object_name ?? e.object_id;
