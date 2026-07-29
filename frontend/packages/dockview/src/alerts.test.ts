import { describe, expect, it, vi } from 'vitest';
import type { Parameters } from 'dockview';
import {
	DockAlerts,
	MUTED_PARAM,
	NOOP_ALERT,
	type AlertPanelApi,
	type AlertView,
} from './alerts.svelte';

/**
 * Panel watchers — and above all, that they are RELEASED.
 *
 * Dock invariant 1 plus `defaultRenderer: 'always'` mean a panel stays mounted while hidden, so a
 * watcher keeps running when its panel is off screen. That is exactly what makes an alert meaningful
 * and exactly what makes an unbounded watcher a leak. These tests are the proof that it is bounded.
 *
 * No DOM here — see `vitest.config.ts`. The records take their collaborators as arguments precisely so
 * this is possible; whether the highlight is actually VISIBLE is a Playwright question.
 */

/** A fake panel api: the four members a record touches, plus a hand-driven visibility emitter. */
function fakeApi(id = 'runs', isVisible = false) {
	const listeners: ((e: { isVisible: boolean }) => void)[] = [];
	const api = {
		id,
		isVisible,
		updateParameters: vi.fn(),
		onDidVisibilityChange: (fn: (e: { isVisible: boolean }) => void) => {
			listeners.push(fn);
			return { dispose: vi.fn() };
		},
	};
	return {
		api: api as unknown as AlertPanelApi,
		/** Drive visibility the way dockview would. */
		show(visible: boolean) {
			api.isVisible = visible;
			for (const fn of listeners) fn({ isVisible: visible });
		},
		updateParameters: api.updateParameters,
	};
}

const acquire = (dock: DockAlerts, id = 'runs', params: Parameters = {}) => {
	const f = fakeApi(id);
	const views: AlertView[] = [];
	const record = dock.acquire(f.api, params, (v) => views.push({ ...v }));
	return { ...f, record, views };
};

describe('the watcher is released — the bounding this feature turns on', () => {
	it('runs the watcher’s teardown when the panel is disposed', () => {
		// THE test. Everything else about alerts is cosmetic next to this: a panel that stays mounted
		// while hidden has nothing else that would ever stop its subscription.
		const dock = new DockAlerts();
		const { record } = acquire(dock);
		const stop = vi.fn();

		record.watch(() => stop);
		expect(stop).not.toHaveBeenCalled();

		record.dispose();
		expect(stop).toHaveBeenCalledTimes(1);
	});

	it('removes the record from the registry, so a leak is a failing count', () => {
		const dock = new DockAlerts();
		const { record } = acquire(dock);
		expect(dock.size).toBe(1);
		record.dispose();
		expect(dock.size).toBe(0);
	});

	it('disposing the DOCK releases every panel’s watcher', () => {
		// The belt to the renderer's braces: a record whose panel dispose was skipped must not outlive
		// the dock itself.
		const dock = new DockAlerts();
		const stops = ['a', 'b', 'c'].map((id) => {
			const stop = vi.fn();
			acquire(dock, id).record.watch(() => stop);
			return stop;
		});

		dock.dispose();
		expect(stops.every((s) => s.mock.calls.length === 1)).toBe(true);
		expect(dock.size).toBe(0);
	});

	it('a second watch() stops the first, so a re-run cannot double-subscribe', () => {
		const dock = new DockAlerts();
		const { record } = acquire(dock);
		const first = vi.fn();
		const second = vi.fn();

		record.watch(() => first);
		record.watch(() => second);
		expect(first).toHaveBeenCalledTimes(1);
		expect(second).not.toHaveBeenCalled();
	});

	it('the returned stopper is idempotent', () => {
		const dock = new DockAlerts();
		const { record } = acquire(dock);
		const stop = vi.fn();
		const off = record.watch(() => stop);

		off();
		off();
		record.dispose();
		expect(stop).toHaveBeenCalledTimes(1);
	});

	it('a watcher declared AFTER dispose is torn down immediately, not leaked', () => {
		const dock = new DockAlerts();
		const { record } = acquire(dock);
		record.dispose();

		const stop = vi.fn();
		record.watch(() => stop);
		// `start` never ran, so there is nothing to stop — and critically nothing left open.
		expect(stop).not.toHaveBeenCalled();
		expect(record.raised).toBe(false);
	});

	it('re-acquiring the same id disposes the previous record rather than orphaning it', () => {
		const dock = new DockAlerts();
		const stop = vi.fn();
		acquire(dock, 'runs').record.watch(() => stop);
		acquire(dock, 'runs');
		expect(stop).toHaveBeenCalledTimes(1);
		expect(dock.size).toBe(1);
	});
});

describe('raising and acknowledging', () => {
	it('raises, and reports the tone and message', () => {
		const dock = new DockAlerts();
		const { record } = acquire(dock);
		record.raise({ tone: 'error', message: 'run failed' });

		expect(record.raised).toBe(true);
		expect(record.tone).toBe('error');
		expect(record.message).toBe('run failed');
		expect(record.count).toBe(1);
	});

	it('counts repeat raises rather than collapsing them', () => {
		const dock = new DockAlerts();
		const { record } = acquire(dock);
		record.raise();
		record.raise();
		expect(record.count).toBe(2);
	});

	it('acknowledge clears the highlight and the count — what the bell does', () => {
		const dock = new DockAlerts();
		const { record } = acquire(dock);
		record.raise({ message: 'x' });
		record.acknowledge();

		expect(record.raised).toBe(false);
		expect(record.count).toBe(0);
		expect(record.message).toBeUndefined();
	});

	it('does NOT auto-clear while the panel is hidden', () => {
		// The point of the feature. A panel you are not looking at is exactly the one whose change must
		// survive until you do look — clearing on a timer while hidden reproduces the silent-change bug.
		vi.useFakeTimers();
		const dock = new DockAlerts();
		const { record } = acquire(dock);
		record.raise();

		vi.advanceTimersByTime(60_000);
		expect(record.raised).toBe(true);
		vi.useRealTimers();
	});

	it('clears on a timer once the panel becomes visible', () => {
		vi.useFakeTimers();
		const dock = new DockAlerts();
		const f = acquire(dock);
		f.record.raise();

		f.show(true);
		vi.advanceTimersByTime(6_000);
		expect(f.record.raised).toBe(false);
		vi.useRealTimers();
	});

	it('a dock-wide raised count is readable without polling', () => {
		const dock = new DockAlerts();
		acquire(dock, 'a').record.raise();
		acquire(dock, 'b').record.raise();
		acquire(dock, 'c');
		expect(dock.raisedCount).toBe(2);
	});
});

describe('mute', () => {
	it('suppresses raises and persists into the panel’s params', () => {
		// Params ride into the serialized layout, so a muted panel stays muted across a save/restore.
		const dock = new DockAlerts();
		const f = acquire(dock);
		f.record.mute(true);
		f.record.raise();

		expect(f.record.raised).toBe(false);
		expect(f.updateParameters).toHaveBeenCalledWith({ [MUTED_PARAM]: true });
	});

	it('restores a muted panel from its params', () => {
		const dock = new DockAlerts();
		const { record } = acquire(dock, 'runs', { [MUTED_PARAM]: true });
		expect(record.muted).toBe(true);
	});

	it('muting an already-raised panel clears it', () => {
		const dock = new DockAlerts();
		const { record } = acquire(dock);
		record.raise();
		record.mute(true);
		expect(record.raised).toBe(false);
	});
});

describe('NOOP_ALERT', () => {
	it('absorbs every call, so a panel never branches on whether alerts exist', () => {
		// Handed to panels when `chrome.alerts` is off. A panel calling `alert.watch(...)` must be
		// harmless rather than a crash, and its returned stopper must still be callable.
		expect(() => {
			const off = NOOP_ALERT.watch(() => () => {});
			NOOP_ALERT.raise({ tone: 'error' });
			NOOP_ALERT.acknowledge();
			NOOP_ALERT.mute(true);
			off();
		}).not.toThrow();
		expect(NOOP_ALERT.raised).toBe(false);
	});
});
