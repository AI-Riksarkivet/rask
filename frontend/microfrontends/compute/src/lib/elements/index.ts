/**
 * The compute zone's custom-element exports — the panels it lends to the global workbench
 * (open_workbench.md). Built by `vite.elements.config.ts` into the zone's own served output;
 * loaded cross-zone by URL, never by import.
 *
 * Registration is manual and GUARDED: the wrappers compile with no `tag` option, so importing
 * this entry twice (compositor reload, HMR) can never hit the "already defined" throw.
 */
import JobsElement from './JobsElement.svelte';

const ELEMENTS: Record<string, CustomElementConstructor> = {
	// @ts-expect-error — `element` exists on CE-compiled components; Svelte's types don't carry it.
	'rask-compute-jobs': JobsElement.element,
};

for (const [tag, ctor] of Object.entries(ELEMENTS)) {
	if (!customElements.get(tag)) customElements.define(tag, ctor);
}
