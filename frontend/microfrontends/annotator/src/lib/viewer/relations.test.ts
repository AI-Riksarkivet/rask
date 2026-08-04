/**
 * Drawing a relation — the half of KIE/DocVQA that had no way to happen.
 *
 * The ontology could DECLARE a relation and the actor could VALIDATE one, but nothing could draw
 * one, so a task declaring a required relation could not be completed by anyone. These pin the
 * interaction, because it is the part with real choices in it:
 *
 *  · CLICKS, not a drag. A drag between two small boxes on a zoomed canvas is a precision task, and
 *    it collides with the marquee/pan gestures the same pointer already owns.
 *  · Arming ADOPTS the inspected annotation as the source. The rail lives in that annotation's
 *    inspector and the sidebar replaces the list with the detail on selection, so asking for a
 *    source click after arming would be both redundant and impossible from the sidebar. This was
 *    found by an e2e that could not click a list which no longer existed.
 *  · While armed, a click is an ENDPOINT and not a selection — otherwise the sidebar moves out from
 *    under the annotator halfway through drawing a link.
 *  · Links are undoable on the SAME stack as shapes. A relation is work; work that Ctrl+Z reaches
 *    only sometimes is work an annotator stops trusting.
 */

import { tableFromArrays } from 'apache-arrow';
import { describe, expect, it, vi } from 'vitest';

vi.mock('svelte-sonner', () => ({
	toast: {
		success: () => {},
		error: () => {},
		message: () => {},
		warning: () => {},
		info: () => {},
	},
}));
vi.mock('./remote/assist.remote', () => ({
	assistRegions: async () => ({ shapes: [] }),
	assistProducers: async () => ({ ok: true, data: { producers: [], default_configured: false } }),
}));
vi.mock('./remote/jobs.remote', () => ({
	submitJob: async () => ({ status: 'unsupported' }),
	jobStatus: async () => null,
}));

import { AnnotatorController } from './annotator.svelte.js';

/** Minimal engine double — relations touch selection, not geometry. */
function ctx() {
	return {
		plugins: {
			arrow: {
				setDeleted: () => {},
				unsetDeleted: () => {},
				clearDeleted: () => {},
				isDeleted: () => false,
				setOverride: () => {},
				clearOverrides: () => {},
				getDirtyOverrides: () => new Map(),
				hasDirtyOverrides: () => false,
				sync: () => {},
				load: () => {},
				setLayerConfig: () => {},
				setHeatmapColumn: () => {},
				setSelected: () => {},
				setMultiSelected: () => {},
				setFieldOverride: () => {},
				adjustOverridesForDelete: () => {},
			},
			interaction: {
				setEditMode: () => {},
				setTool: () => {},
				setBrushOptions: () => {},
				getSelectedSet: () => new Set<number>(),
				cvCapable: false,
				setImageSource: () => {},
				select: () => {},
				clearSelection: () => {},
				setSelected: () => {},
				handleKeyDown: () => {},
				set onCommit(_fn: unknown) {},
				set onChange(_fn: unknown) {},
				set onSelect(_fn: unknown) {},
				set onDirtyChange(_fn: unknown) {},
				set onCvToolReady(_fn: unknown) {},
				set onMagneticSnap(_fn: unknown) {},
			},
			image: { onViewportChange: undefined, zoomPercent: 100 },
		},
	};
}

const TABLE = tableFromArrays({
	id: ['k1', 'v1', 'v2'],
	shape_type: ['bbox', 'bbox', 'bbox'],
	label: ['key', 'value', 'value'],
});

function attached() {
	const c = new AnnotatorController();
	// eslint-disable-next-line @typescript-eslint/no-explicit-any -- structural double, not a PixiContext
	c.attach(ctx() as any, TABLE);
	c.relationNames = ['answers'];
	return c;
}

describe('drawing a relation', () => {
	it('arming ADOPTS the inspected annotation as the source', () => {
		const c = attached();
		c.select(0);
		c.toggleLinkMode('answers');

		expect(c.pendingLinkFrom, 'arming did not adopt the shape being inspected').toBe('k1');

		// One more click completes it — the rail says "link THIS one", so the next click is a target.
		c.select(1);
		expect(c.links).toEqual([{ name: 'answers', from_shape: 'k1', to_shape: 'v1' }]);
	});

	it('with NOTHING selected, the first click is the source', () => {
		// The canvas-first path: arm from a task with no row inspected and it still works.
		const c = attached();
		c.toggleLinkMode('answers');

		c.select(0);
		expect(c.pendingLinkFrom, 'the first click did not arm an endpoint').toBe('k1');
		expect(c.links, 'a link was created from one click').toHaveLength(0);

		c.select(1);
		expect(c.links).toEqual([{ name: 'answers', from_shape: 'k1', to_shape: 'v1' }]);
		expect(c.pendingLinkFrom, 'the pending endpoint survived completion').toBeNull();
	});

	it('IDs, never row indices', () => {
		// The draft is replaced whole on every save and rows shift when one is deleted, so an index
		// would silently re-point at a different shape.
		const c = attached();
		c.toggleLinkMode('answers');
		c.select(0);
		c.select(2);

		expect(c.links[0]!.from_shape).toBe('k1');
		expect(c.links[0]!.to_shape).toBe('v2');
	});

	it('a click while armed does NOT change the selection', () => {
		const c = attached();
		c.select(2);
		c.toggleLinkMode('answers');

		c.select(0);
		expect(c.selectedIndex, 'the sidebar moved out from under a half-drawn link').toBe(2);
	});

	it('clicking the source again CANCELS rather than self-linking', () => {
		const c = attached();
		c.toggleLinkMode('answers');
		c.select(0);
		c.select(0);

		expect(c.links, 'a shape was linked to itself').toHaveLength(0);
		expect(c.pendingLinkFrom).toBeNull();
	});

	it('disarming clears a half-drawn link', () => {
		const c = attached();
		c.toggleLinkMode('answers');
		c.select(0);
		c.toggleLinkMode('answers');

		expect(c.linkMode).toBeNull();
		expect(c.pendingLinkFrom, 'a stale endpoint survived disarming').toBeNull();
	});

	it('an identical link is not added twice', () => {
		const c = attached();
		c.toggleLinkMode('answers');
		c.select(0);
		c.select(1);
		c.select(0);
		c.select(1);

		expect(c.links).toHaveLength(1);
	});

	it('links are undoable on the SAME stack as shapes', () => {
		const c = attached();
		c.toggleLinkMode('answers');
		c.select(0);
		c.select(1);
		expect(c.canUndo, 'drawing a link pushed no undo op').toBe(true);

		c.undo();
		expect(c.links, 'undo did not remove the link').toHaveLength(0);

		c.redo();
		expect(c.links, 'redo did not restore the link').toHaveLength(1);
	});

	it('a DELETED link is restored by undo', () => {
		const c = attached();
		c.addLink({ name: 'answers', from_shape: 'k1', to_shape: 'v1' });
		c.removeLink({ name: 'answers', from_shape: 'k1', to_shape: 'v1' });
		expect(c.links).toHaveLength(0);

		c.undo();
		expect(c.links, 'undo did not restore the deleted link').toHaveLength(1);

		c.redo();
		expect(c.links, 'redo did not re-delete it').toHaveLength(0);
	});

	it('undoing a link does not consume the reselect as an endpoint', () => {
		// `_apply` reselects the row an op touched. Routing that through the link-intercepting
		// `select()` would feed the undone row into a half-drawn link — a self-inflicted loop.
		const c = attached();
		c.updateField(0, 'label', 'renamed');
		c.toggleLinkMode('answers');

		c.undo(); // reverses the FIELD edit, which reselects row 0
		expect(c.pendingLinkFrom, 'an internal reselect armed a link endpoint').toBeNull();
	});

	it('linksFor finds a shape at EITHER end', () => {
		const c = attached();
		c.addLink({ name: 'answers', from_shape: 'k1', to_shape: 'v1' });

		expect(c.linksFor('k1')).toHaveLength(1);
		expect(c.linksFor('v1'), 'a link was invisible from its target').toHaveLength(1);
		expect(c.linksFor('v2')).toHaveLength(0);
	});

	it('a link marks the canvas dirty, so Save is offered', () => {
		const c = attached();
		expect(c.dirty).toBe(false);
		c.addLink({ name: 'answers', from_shape: 'k1', to_shape: 'v1' });
		expect(c.dirty, 'a drawn relation left the canvas looking saved').toBe(true);
	});
});
