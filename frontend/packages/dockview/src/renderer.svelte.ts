/**
 * The Svelte↔dockview seam: an `IContentRenderer` whose body is a mounted Svelte component.
 *
 * THE INVARIANT this file exists to hold — verified against dockview@7.0.4's SHIPPED bundle
 * (`dockview-core/dist/package/main.esm.mjs`, which `dockview` re-exports), not its docs:
 *
 *   A panel's IContentRenderer is constructed exactly ONCE — in the `DockviewPanelModel` constructor,
 *   via `accessor.options.createComponent({id, name})` — and is disposed only from
 *   `DockviewComponent._doRemovePanel` when `skipDispose` is falsy. Every MOVE path
 *   (`_doMoveGroupOrPanel` → `sourceGroup.model.removePanel(...)` → `doRemovePanel`) re-parents that
 *   same instance and disposes only the group's per-panel event subscriptions (`_panelDisposables`),
 *   never the panel.
 *
 * So `mount()` here runs once and `unmount()` runs once, and a panel's live state — a running
 * interval, a pan/zoom viewport, an open EventSource, a scroll offset — survives being dragged into
 * another group, to the far edge of the layout, and into a floating group. Any future change that
 * mounts outside `init()` or unmounts outside `dispose()` breaks that and is wrong.
 */
import { mount, unmount } from 'svelte';
// `DockviewIDisposable`, not `IDisposable`: dockview exports its Event/Emitter/Disposable trio under a
// `Dockview` prefix on purpose — "to be a good citizen ... to prevent accidental use by others"
// (dockview's index.d.ts). The unprefixed names are internal and resolve to nothing here.
import type {
	DockviewIDisposable,
	GroupPanelPartInitParameters,
	IContentRenderer,
	Parameters,
} from 'dockview';
import { NOOP_ALERT, type DockAlerts, type PanelAlert } from './alerts.svelte';
import type { AnyPanelComponent, PanelComponent } from './types';

export class SveltePanelRenderer implements IContentRenderer {
	readonly element: HTMLElement;

	#component: PanelComponent;
	#context: Map<unknown, unknown>;
	/**
	 * Stable identity, MUTATED in place — never reassigned. The mounted component captured this exact
	 * reference as its `params` prop, so replacing the object would leave it reading a detached one.
	 */
	#params: Parameters = $state<Parameters>({});
	#instance: Record<string, unknown> | null = null;
	#paramSubscription: DockviewIDisposable | null = null;
	/** The dock-wide alert registry, or null when `chrome.alerts` is off. */
	#alerts: DockAlerts | null;
	/** This panel's record. Released FIRST in dispose(), which is what bounds every watcher. */
	#alert: PanelAlert | null = null;

	constructor(
		component: AnyPanelComponent,
		context: Map<unknown, unknown>,
		alerts: DockAlerts | null = null,
	) {
		this.#alerts = alerts;
		// The registry stores `AnyPanelComponent` so BOTH shapes a zone might write are assignable: a
		// panel declaring `PanelProps` (component props are contravariant, and `never` is the bottom
		// type), and one declaring no props at all — which every panel in this repo currently does, and
		// which `svelte2tsx` compiles to a type that REJECTS extra props. Mounting needs the widened
		// form back; this is that one widening, and the only cast in the package. Sound at runtime
		// either way: a component declaring no props simply ignores the ones it is handed.
		this.#component = component as PanelComponent;
		this.#context = context;
		this.element = document.createElement('div');
		this.element.className = 'rask-dock-panel';
	}

	init(parameters: GroupPanelPartInitParameters): void {
		this.#sync(parameters.params);
		this.#paramSubscription = parameters.api.onDidParametersChange((next) => this.#sync(next));
		// The ONLY DOM the alert owns. A data-attribute rather than a class: it carries the tone as its
		// value and cannot collide with the className set in the constructor.
		this.#alert =
			this.#alerts?.acquire(parameters.api, parameters.params, (view) => {
				if (view.raised) this.element.dataset.alert = view.tone;
				else delete this.element.dataset.alert;
			}) ?? null;
		this.#instance = mount(this.#component, {
			target: this.element,
			context: this.#context,
			props: {
				params: this.#params,
				api: parameters.api,
				containerApi: parameters.containerApi,
				alert: this.#alert ?? NOOP_ALERT,
			},
		}) as Record<string, unknown>;
	}

	dispose(): void {
		// FIRST, before the component unmounts: this runs the watcher's teardown, which is the whole
		// bounding story. A panel stays mounted while hidden, so nothing else would ever stop it.
		this.#alert?.dispose();
		this.#alert = null;
		this.#paramSubscription?.dispose();
		this.#paramSubscription = null;
		if (this.#instance !== null) {
			// Fire-and-forget: `unmount` returns a promise that settles after outros, and dockview's
			// dispose() is synchronous. There are no outros on a panel body, so nothing is awaited.
			void unmount(this.#instance);
			this.#instance = null;
		}
	}

	/** Reconcile the params object IN PLACE, so its identity — and the component's binding — survives. */
	#sync(next: Parameters): void {
		for (const key of Object.keys(this.#params)) {
			if (!(key in next)) delete this.#params[key];
		}
		Object.assign(this.#params, next);
	}
}

/**
 * The renderer used when a persisted layout names a panel the registry no longer has.
 *
 * A saved layout outlives the code that wrote it: rename a registry key and every stored layout
 * referencing it points at nothing. Throwing here would abort `fromJSON` and brick the whole dock
 * over one stale panel, so this renders a plain, self-explaining placeholder instead — the user
 * closes it and their layout is repaired. Deliberately zero-Svelte: it must not be able to fail.
 */
export class MissingPanelRenderer implements IContentRenderer {
	readonly element: HTMLElement;

	constructor(name: string) {
		this.element = document.createElement('div');
		this.element.className = 'rask-dock-panel rask-dock-missing';
		this.element.textContent = `No panel is registered as “${name}”. Close this tab to drop it from your saved layout.`;
	}

	init(): void {
		// Nothing to initialise — the message is fixed at construction.
	}
}
