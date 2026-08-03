/**
 * The compute zone's custom-element exports — the panels it lends to the global workbench
 * (open_workbench.md). Built by `vite.elements.config.ts` into the zone's own served output;
 * loaded cross-zone by URL, never by import.
 *
 * Registration is manual and GUARDED: the wrappers compile with no `tag` option, so importing
 * this entry twice (compositor reload, HMR) can never hit the "already defined" throw.
 */
import elementsCss from './elements.css?inline';
import ActorsElement from './ActorsElement.svelte';
import ClusterElement from './ClusterElement.svelte';
import JobsElement from './JobsElement.svelte';
import LogsElement from './LogsElement.svelte';
import OverviewElement from './OverviewElement.svelte';
import ServeElement from './ServeElement.svelte';

/** The elements' OWN Tailwind utilities (see elements.css), injected once. */
if (document.getElementById('rask-compute-elements-utilities') === null) {
	const style = document.createElement('style');
	style.id = 'rask-compute-elements-utilities';
	style.textContent = elementsCss;
	document.head.append(style);
}

const ELEMENTS: Record<string, CustomElementConstructor> = {
	// @ts-expect-error — `element` exists on CE-compiled components; Svelte's types don't carry it.
	'rask-compute-jobs': JobsElement.element,
	// @ts-expect-error — same.
	'rask-compute-cluster': ClusterElement.element,
	// @ts-expect-error — same.
	'rask-compute-actors': ActorsElement.element,
	// @ts-expect-error — same.
	'rask-compute-serve': ServeElement.element,
	// @ts-expect-error — same.
	'rask-compute-overview': OverviewElement.element,
	// @ts-expect-error — same.
	'rask-compute-logs': LogsElement.element,
};

for (const [tag, ctor] of Object.entries(ELEMENTS)) {
	if (!customElements.get(tag)) customElements.define(tag, ctor);
}
