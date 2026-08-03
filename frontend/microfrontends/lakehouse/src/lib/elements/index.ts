/**
 * The lakehouse zone's custom-element exports for the global workbench (open_workbench.md).
 * Registration is manual and GUARDED — wrappers compile with no `tag`, so a double load never
 * hits the "already defined" throw. The lineage elements share ONE LineageState via ./store.
 *
 * VENDOR CSS: the graph element mounts xyflow, whose stylesheet the zones import via app.css —
 * a path that does not exist for an element loaded cross-zone. The sheets are bundled `?inline`
 * and injected ONCE under `@layer base`, the same layer discipline the zones use, so the host
 * page's Tailwind utilities still out-rank them (rask-styling's third-party-sheet rule).
 */
import elementsCss from './elements.css?inline';
import xyflowCss from '@xyflow/svelte/dist/style.css?inline';
import flowCss from '@rask/flow/styles.css?inline';
import AccessElement from './AccessElement.svelte';
import AuditElement from './AuditElement.svelte';
import ColumnsElement from './ColumnsElement.svelte';
import DatasetsElement from './DatasetsElement.svelte';
import EventsElement from './EventsElement.svelte';
import GraphElement from './GraphElement.svelte';
import OpsElement from './OpsElement.svelte';
import RunsElement from './RunsElement.svelte';

/** The elements' OWN Tailwind utilities (see elements.css) — injected RAW, not layer-wrapped:
 *  Tailwind already emits its own layer structure, and wrapping utilities in layer(base) would
 *  put them BELOW the host's, inverting specificity. */
function injectUtilities(id: string, css: string): void {
	if (document.getElementById(id) !== null) return;
	const style = document.createElement('style');
	style.id = id;
	style.textContent = css;
	document.head.append(style);
}

function injectOnce(id: string, css: string): void {
	if (document.getElementById(id) !== null) return;
	const style = document.createElement('style');
	style.id = id;
	style.textContent = `@layer base{${css}}`;
	document.head.append(style);
}
injectOnce('rask-lakehouse-elements-flow-css', xyflowCss + flowCss);
injectUtilities('rask-lakehouse-elements-utilities', elementsCss);

const ELEMENTS: Record<string, CustomElementConstructor> = {
	// @ts-expect-error — `element` exists on CE-compiled components; Svelte's types don't carry it.
	'rask-lakehouse-runs': RunsElement.element,
	// @ts-expect-error — same.
	'rask-lakehouse-events': EventsElement.element,
	// @ts-expect-error — same.
	'rask-lakehouse-graph': GraphElement.element,
	// @ts-expect-error — same.
	'rask-lakehouse-datasets': DatasetsElement.element,
	// @ts-expect-error — same.
	'rask-lakehouse-audit': AuditElement.element,
	// @ts-expect-error — same.
	'rask-lakehouse-access': AccessElement.element,
	// @ts-expect-error — same.
	'rask-lakehouse-ops': OpsElement.element,
	// @ts-expect-error — same.
	'rask-lakehouse-columns': ColumnsElement.element,
};

for (const [tag, ctor] of Object.entries(ELEMENTS)) {
	if (!customElements.get(tag)) customElements.define(tag, ctor);
}
