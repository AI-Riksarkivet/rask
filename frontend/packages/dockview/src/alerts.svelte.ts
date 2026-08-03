/**
 * Panel watchers — the bell, and highlighting the WHOLE panel.
 *
 * A panel declares a watcher; when the data behind it moves, the dock raises an alert. The owner's
 * specification is that the **whole panel** highlights, explicitly NOT a dot on the tab: a background
 * panel changing silently IS the failure, and a 6px dot on a tab nobody is looking at reproduces it.
 *
 * WHY THIS IS MEANINGFUL AND WHY IT MUST BE BOUNDED — the same fact, twice. Panels stay mounted while
 * hidden (dock invariant 1 + `defaultRenderer: 'always'`), so a watcher keeps running when its panel
 * is not visible. That is exactly what makes the alert worth having, and exactly what makes an
 * unbounded watcher a leak. Every subscription here is released from `dispose()`, which dockview calls
 * when a panel is genuinely removed and never on a move.
 *
 * THE SEAM. Alert state has to be readable by two things mounted by two SEPARATE dockview renderer
 * factories, in different component trees: `SveltePanelRenderer` (to put the highlight on the panel
 * element) and `GroupActions` (to render the bell). There is no shared Svelte parent, and context
 * cannot bridge them — `header-actions.svelte.ts` mounts `GroupActions` with NO context map at all, so
 * a context getter would find an empty tree. So `DockAlerts` is created at `Dock.svelte` init and
 * closed over by both factories.
 *
 * That works because Svelte 5 signals are GLOBAL, not tree-scoped: `mount()` starts a new effect root
 * but shares the signal graph. `renderer.svelte.ts` already depends on this — its `#params` is
 * `$state` written from a dockview event callback and read by a component in a `mount()` root.
 *
 * THE ONE RULE for the `apply` callback: it may write DOM and must never write `$state`. It runs
 * inside an effect, so a state write there is a cycle.
 */
import type { DockviewIDisposable, DockviewPanelApi, Parameters } from 'dockview';

export type AlertTone = 'info' | 'warn' | 'error';

export interface AlertDetail {
	readonly tone?: AlertTone;
	readonly message?: string;
}

/** Idempotent teardown for a watcher. Safe to call twice; safe to call after dispose. */
export type StopWatching = () => void;

/** What the renderer needs in order to paint the highlight. */
export interface AlertView {
	readonly raised: boolean;
	readonly tone: AlertTone;
}

/** A panel's own alert channel — pre-bound, so there is no id to pass and no way to raise on the
 *  wrong panel. Panels receive one as `PanelProps.alert`. */
export interface PanelAlertApi {
	/** Raise. A no-op while muted. A repeat raise restarts the highlight and increments `count`. */
	raise(detail?: AlertDetail): void;
	/** Clear the highlight and the count — what the bell does. */
	acknowledge(): void;
	/** Stop raising. Persisted in the panel's params, so it survives a layout save/restore. */
	mute(muted: boolean): void;
	/**
	 * Declare the watcher. `start` runs immediately and returns its own teardown. A second `watch()`
	 * stops the first, so a double-mount cannot double-subscribe. The returned stopper is idempotent
	 * AND is run on panel dispose — that is the bounding this feature turns on.
	 */
	watch(start: (raise: (detail?: AlertDetail) => void) => StopWatching | void): StopWatching;
	readonly raised: boolean;
	readonly muted: boolean;
	readonly count: number;
	readonly message: string | undefined;
}

/** The slice of `DockviewPanelApi` a record needs — structural, so a test fakes it in 20 lines. */
export type AlertPanelApi = Pick<
	DockviewPanelApi,
	'id' | 'isVisible' | 'onDidVisibilityChange' | 'updateParameters'
>;

/** Mute rides in the panel's params so it survives a layout round trip; dockview serializes them. */
export const MUTED_PARAM = '__raskAlertMuted';

/** How long the highlight lingers once the panel is actually on screen. */
const DEFAULT_LINGER_MS = 6000;

/** Handed to panels when the dock has `chrome.alerts === false`, so a panel never branches on it. */
export const NOOP_ALERT: PanelAlertApi = {
	raise: () => {},
	acknowledge: () => {},
	mute: () => {},
	watch: () => () => {},
	raised: false,
	muted: false,
	count: 0,
	message: undefined,
};

export class PanelAlert implements PanelAlertApi {
	readonly id: string;
	raised = $state(false);
	muted = $state(false);
	/** True once a watcher is declared — the bell only appears for panels that actually watch. */
	watching = $state(false);
	count = $state(0);
	tone = $state<AlertTone>('info');
	message = $state<string | undefined>(undefined);

	#api: AlertPanelApi;
	#lingerMs: number;
	#release: () => void;
	#stopWatcher: StopWatching | null = null;
	#visibility: DockviewIDisposable | null = null;
	#timer: ReturnType<typeof setTimeout> | null = null;
	#stopEffects: (() => void) | null = null;
	#disposed = false;

	constructor(
		api: AlertPanelApi,
		params: Parameters,
		lingerMs: number,
		apply: (view: AlertView) => void,
		release: () => void,
	) {
		this.id = api.id;
		this.#api = api;
		this.#lingerMs = lingerMs;
		this.#release = release;
		this.muted = params[MUTED_PARAM] === true;
		// The highlight clears on a timer only once the panel is VISIBLE. Clearing while hidden would
		// defeat the point — the alert exists to survive until someone looks at it.
		this.#visibility = api.onDidVisibilityChange(({ isVisible }) => {
			if (isVisible) this.#startLinger();
			else this.#clearTimer();
		});
		// `$effect.root`, not a bare `$effect`: this constructor runs from a dockview renderer, outside
		// any component's effect context, where a bare effect throws `effect_orphan`. The returned
		// teardown is kept and run from dispose() — an unowned root would otherwise never be collected.
		this.#stopEffects = $effect.root(() => {
			$effect(() => {
				apply({ raised: this.raised, tone: this.tone });
			});
		});
	}

	raise(detail?: AlertDetail): void {
		if (this.#disposed || this.muted) return;
		this.count += 1;
		this.tone = detail?.tone ?? 'info';
		this.message = detail?.message;
		this.raised = true;
		if (this.#api.isVisible) this.#startLinger();
	}

	acknowledge(): void {
		this.#clearTimer();
		this.raised = false;
		this.count = 0;
		this.message = undefined;
	}

	mute(muted: boolean): void {
		if (this.muted === muted) return;
		this.muted = muted;
		if (muted) this.acknowledge();
		this.#api.updateParameters({ [MUTED_PARAM]: muted ? true : undefined });
	}

	watch(start: (raise: (detail?: AlertDetail) => void) => StopWatching | void): StopWatching {
		// A second watch() replaces the first. Without this a component that re-runs its setup would
		// silently hold two subscriptions, and only the second would ever be torn down.
		this.#stopWatcher?.();
		this.watching = true;
		const stop = this.#disposed ? undefined : start((detail) => this.raise(detail));
		let stopped = false;
		const wrapped = (): void => {
			if (stopped) return;
			stopped = true;
			if (this.#stopWatcher === wrapped) this.#stopWatcher = null;
			stop?.();
		};
		this.#stopWatcher = wrapped;
		// Declared after the panel was already disposed — start it, then stop it immediately, so the
		// caller's teardown still runs rather than leaking whatever `start` opened.
		if (this.#disposed) wrapped();
		return wrapped;
	}

	dispose(): void {
		if (this.#disposed) return;
		this.#disposed = true;
		this.#clearTimer();
		this.#stopWatcher?.();
		this.#stopWatcher = null;
		this.#visibility?.dispose();
		this.#visibility = null;
		this.#stopEffects?.();
		this.#stopEffects = null;
		this.#release();
	}

	#startLinger(): void {
		this.#clearTimer();
		if (!this.raised || this.#lingerMs <= 0) return;
		this.#timer = setTimeout(() => {
			this.#timer = null;
			this.acknowledge();
		}, this.#lingerMs);
	}

	#clearTimer(): void {
		if (this.#timer !== null) clearTimeout(this.#timer);
		this.#timer = null;
	}
}

/** Every panel's alert record for one dock, keyed by panel id. Created at `Dock.svelte` init. */
export class DockAlerts {
	#byId: Record<string, PanelAlert> = $state({});
	#lingerMs: number;

	constructor(lingerMs: number = DEFAULT_LINGER_MS) {
		this.#lingerMs = lingerMs;
	}

	/** Live record count — the release assertions read this, so a leak is a failing test. */
	get size(): number {
		return Object.keys(this.#byId).length;
	}

	/** How many panels are alerting anywhere in the dock. Reactive. */
	get raisedCount(): number {
		return Object.values(this.#byId).filter((r) => r.raised).length;
	}

	get(id: string): PanelAlert | undefined {
		return this.#byId[id];
	}

	acquire(api: AlertPanelApi, params: Parameters, apply: (view: AlertView) => void): PanelAlert {
		// Defensive: a re-acquire for the same id disposes the previous record rather than orphaning it.
		this.#byId[api.id]?.dispose();
		const record = new PanelAlert(api, params, this.#lingerMs, apply, () => {
			// Only the CURRENT record for this id may remove itself — a late dispose from a replaced
			// record must not delete its successor.
			if (this.#byId[api.id] === record) delete this.#byId[api.id];
		});
		this.#byId[api.id] = record;
		return record;
	}

	/** The dock-teardown belt: every record released even if a panel dispose was skipped. */
	dispose(): void {
		for (const record of Object.values(this.#byId)) record.dispose();
	}
}
