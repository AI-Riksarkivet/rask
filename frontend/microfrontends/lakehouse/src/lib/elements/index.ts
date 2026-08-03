/**
 * The lakehouse zone's custom-element exports for the global workbench (open_workbench.md).
 * Registration is manual and GUARDED — wrappers compile with no `tag`, so a double load never
 * hits the "already defined" throw. All elements share ONE LineageState via ./store.
 */
import EventsElement from './EventsElement.svelte';
import RunsElement from './RunsElement.svelte';

const ELEMENTS: Record<string, CustomElementConstructor> = {
	// @ts-expect-error — `element` exists on CE-compiled components; Svelte's types don't carry it.
	'rask-lakehouse-runs': RunsElement.element,
	// @ts-expect-error — same.
	'rask-lakehouse-events': EventsElement.element,
};

for (const [tag, ctor] of Object.entries(ELEMENTS)) {
	if (!customElements.get(tag)) customElements.define(tag, ctor);
}
