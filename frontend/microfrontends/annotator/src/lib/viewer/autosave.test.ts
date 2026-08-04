/**
 * Autosave — and specifically what it does when it CANNOT save.
 *
 * This half of the work was deferred because it needed a real answer to one question, and the wrong
 * answer is invisible: a manual save that conflicts reloads the server state and DROPS the pending
 * edits. That is defensible when a person pressed Save and is told to their face. Doing the same on
 * a four-second timer would discard someone's work in the background while they were still typing,
 * and they would find out by noticing their labels were gone.
 *
 * So autosave HALTS on a conflict and leaves the edits alone. Resolving a conflict is a decision,
 * and decisions belong to the annotator. Everything below is that rule and its consequences.
 */

import { tableFromArrays } from 'apache-arrow';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

const postSave = vi.fn();

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
// `save()` ends by snapshotting the unit into the task draft, through TWO dynamic `$lib` imports.
// Unmocked they reject, the whole save lands in its catch, and every assertion below reads a
// transport failure instead of the thing it is testing.
vi.mock('$lib/labeling/review-selection.svelte', () => ({
	reviewSelection: { taskId: null, total: 0 },
}));
vi.mock('$lib/projects/draft-sync', () => ({ syncTaskDraft: async () => null }));
vi.mock('@rask/labeling/annotations-client', async (orig) => {
	const actual = (await orig()) as Record<string, unknown>;
	return {
		...actual,
		postSave: (...args: unknown[]) => postSave(...args),
		// The canvas reloads from the server after a save; keep the same table so the test's
		// assertions are about SAVING, not about reconciliation.
		loadAnnotations: async () => ({ table: TABLE, version: 2, saveUrl: '/save' }),
	};
});

import { AnnotatorController } from './annotator.svelte.js';

const TABLE = tableFromArrays({
	id: ['a1', 'a2'],
	shape_type: ['bbox', 'bbox'],
	label: ['portrait', 'stamp'],
});

function ctx() {
	const noop = () => {};
	return {
		plugins: {
			arrow: {
				setDeleted: noop,
				unsetDeleted: noop,
				clearDeleted: noop,
				isDeleted: () => false,
				setOverride: noop,
				clearOverrides: noop,
				getDirtyOverrides: () => new Map(),
				hasDirtyOverrides: () => false,
				sync: noop,
				load: noop,
				setLayerConfig: noop,
				setHeatmapColumn: noop,
				setSelected: noop,
				setMultiSelected: noop,
				setFieldOverride: noop,
				adjustOverridesForDelete: noop,
			},
			interaction: {
				setEditMode: noop,
				setTool: noop,
				setBrushOptions: noop,
				getSelectedSet: () => new Set<number>(),
				cvCapable: false,
				setImageSource: noop,
				select: noop,
				clearSelection: noop,
				setSelected: noop,
				handleKeyDown: noop,
				set onCommit(_f: unknown) {},
				set onChange(_f: unknown) {},
				set onSelect(_f: unknown) {},
				set onDirtyChange(_f: unknown) {},
				set onCvToolReady(_f: unknown) {},
				set onMagneticSnap(_f: unknown) {},
			},
			image: { onViewportChange: undefined, zoomPercent: 100 },
		},
	};
}

/** A controller with a save URL, one pending edit, and autosave armed. */
function dirtyCanvas() {
	const c = new AnnotatorController();
	// eslint-disable-next-line @typescript-eslint/no-explicit-any -- structural double, not a PixiContext
	c.attach(ctx() as any, TABLE, '/save', 1);
	c.startAutosave();
	c.updateField(0, 'label', 'edited');
	return c;
}

/** Run the debounce out and let the save's promise chain settle. */
async function idle(): Promise<void> {
	await vi.advanceTimersByTimeAsync(AnnotatorController.AUTOSAVE_IDLE_MS + 10);
	await vi.waitFor(() => expect(postSave).toHaveBeenCalled());
}

beforeEach(() => {
	vi.useFakeTimers();
	postSave.mockReset();
	postSave.mockResolvedValue({ status: 'ok' });
});
afterEach(() => {
	vi.useRealTimers();
});

describe('autosave', () => {
	it('is OFF until someone starts it', async () => {
		// The ad-hoc `?keys=` canvas keeps explicit save. A background writer nobody asked for is not
		// a feature — it writes to units people are only LOOKING at.
		const c = new AnnotatorController();
		// eslint-disable-next-line @typescript-eslint/no-explicit-any -- structural double
		c.attach(ctx() as any, TABLE, '/save', 1);
		c.updateField(0, 'label', 'edited');
		c.scheduleAutosave();

		await vi.advanceTimersByTimeAsync(AnnotatorController.AUTOSAVE_IDLE_MS * 3);
		expect(postSave, 'a canvas nobody armed saved itself').not.toHaveBeenCalled();
	});

	it('saves once the edits go quiet', async () => {
		const c = dirtyCanvas();
		c.scheduleAutosave();
		await idle();
		expect(postSave).toHaveBeenCalledTimes(1);
	});

	it('DEBOUNCES a burst into one write', async () => {
		// The transport is a whole-payload PUT guarded by a version. One write per keystroke is not
		// just wasteful — it is a conflict generator against anyone else on the same unit.
		const c = dirtyCanvas();
		for (let i = 0; i < 5; i++) {
			c.updateField(0, 'label', `edit-${i}`);
			c.scheduleAutosave();
			await vi.advanceTimersByTimeAsync(500);
		}
		expect(postSave, 'a write escaped mid-burst').not.toHaveBeenCalled();

		await idle();
		expect(postSave).toHaveBeenCalledTimes(1);
	});

	it('a CONFLICT halts autosave and KEEPS the edits', async () => {
		// The whole reason this was deferred. A manual save reloads and drops the pending edits;
		// doing that on a timer is silent data loss.
		postSave.mockResolvedValue({ status: 'conflict' });
		const c = dirtyCanvas();
		c.scheduleAutosave();
		await idle();

		expect(c.autosaveHalted, 'autosave carried on after a conflict').not.toBeNull();
		expect(c.dirty, 'the annotator lost their edits to a background save').toBe(true);
		expect(c.rows[0]!.label, 'the edit was reverted by a reload nobody asked for').toBe('edited');
	});

	it('a halted autosave does NOT keep retrying', async () => {
		postSave.mockResolvedValue({ status: 'conflict' });
		const c = dirtyCanvas();
		c.scheduleAutosave();
		await idle();
		const afterHalt = postSave.mock.calls.length;

		c.updateField(0, 'label', 'more');
		c.scheduleAutosave();
		await vi.advanceTimersByTimeAsync(AnnotatorController.AUTOSAVE_IDLE_MS * 3);

		expect(postSave.mock.calls.length, 'a halted autosave retried anyway').toBe(afterHalt);
	});

	it('a MANUAL save clears the halt — the conflict has been looked at', async () => {
		postSave.mockResolvedValue({ status: 'conflict' });
		const c = dirtyCanvas();
		c.scheduleAutosave();
		await idle();
		expect(c.autosaveHalted).not.toBeNull();

		postSave.mockResolvedValue({ status: 'ok' });
		await c.save();

		expect(c.autosaveHalted, 'the halt survived a successful manual save').toBeNull();
	});

	it('a TRANSPORT failure halts too', async () => {
		// Retrying a broken connection every four seconds buries the real error under a hundred
		// identical toasts.
		postSave.mockRejectedValue(new Error('network down'));
		const c = dirtyCanvas();
		c.scheduleAutosave();
		await idle();

		expect(c.autosaveHalted).toContain('network down');
	});

	it('stopping cancels a pending write', async () => {
		// The timer must die with the canvas, or it fires into a unit nobody is looking at.
		const c = dirtyCanvas();
		c.scheduleAutosave();
		await vi.advanceTimersByTimeAsync(AnnotatorController.AUTOSAVE_IDLE_MS / 2);
		c.stopAutosave();

		await vi.advanceTimersByTimeAsync(AnnotatorController.AUTOSAVE_IDLE_MS * 3);
		expect(postSave, 'a stopped canvas still wrote').not.toHaveBeenCalled();
	});

	it('the status line says ONE thing at a time', async () => {
		const c = new AnnotatorController();
		// eslint-disable-next-line @typescript-eslint/no-explicit-any -- structural double
		c.attach(ctx() as any, TABLE, '/save', 1);
		expect(c.saveStatus).toBe('');

		c.updateField(0, 'label', 'edited');
		expect(c.saveStatus, 'an unarmed canvas claimed it was autosaving').toBe('unsaved');

		c.startAutosave();
		expect(c.saveStatus).toBe('unsaved — autosaving');

		c.scheduleAutosave();
		await idle();
		expect(c.saveStatus).toBe('saved');
	});

	it('a halt is what the status line SHOWS — silence would be the failure', async () => {
		postSave.mockResolvedValue({ status: 'conflict' });
		const c = dirtyCanvas();
		c.scheduleAutosave();
		await idle();

		expect(c.saveStatus).toContain('conflict');
	});
});
