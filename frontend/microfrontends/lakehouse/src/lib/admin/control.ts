import * as v from 'valibot';

import type { components } from '@rask/api/generated/catalog';

// The control-plane change-event wire contract (services/catalog endpoints/events.py) — parsed (not cast)
// at the BFF boundary so a drift from the backend throws here rather than lying downstream (the @rask/api
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
} satisfies Record<keyof EventWire, unknown>);
export type ControlEvent = v.InferOutput<typeof ControlEventSchema>;

export const EventsPageSchema = v.object({
	events: v.array(ControlEventSchema),
	cursor: v.number(),
	reset: v.boolean(),
} satisfies Record<keyof PageWire, unknown>);
