<script lang="ts">
	/**
	 * The dock host. Owns one `DockviewComponent`, hands the consumer the REAL `DockviewApi`, and
	 * gets out of the way — there is no wrapper over dockview's own methods, deliberately. A zone
	 * calls `api.addPanel(...)`, `api.toJSON()`, `api.onDidActivePanelChange(...)` exactly as the
	 * dockview docs describe them, so there is one API to learn rather than two.
	 *
	 * Zone-agnostic by construction: everything zone-shaped — which panels exist, where a layout is
	 * stored, what context the panels need — arrives as a prop.
	 */
	import { getAllContexts, onMount, untrack } from 'svelte';
	import { DockviewComponent } from 'dockview';
	import type { DockviewApi, DockviewOptions } from 'dockview';
	import { MissingPanelRenderer, SveltePanelRenderer } from './renderer.svelte';
	import { LayoutAutosave } from './persistence';
	import { raskDockTheme } from './theme';
	import { EDGE_DROP, resolveChrome, type DockChrome, type DockChromeOptions } from './chrome';
	import { makeHeaderActions } from './header-actions.svelte';
	import { makeTabContextMenu } from './context-menu';
	import type { LayoutRead, LayoutStore, PanelRegistry } from './types';

	interface Props {
		/** The zone's panel catalogue, keyed by the `component` string passed to `api.addPanel`. */
		panels: PanelRegistry;
		/**
		 * Handed the live `DockviewApi` once any stored layout has been restored. `restored` is false
		 * when the user genuinely had no saved layout — seed your default panels then, and only then,
		 * or you will overwrite the layout that is about to load.
		 */
		onready?: (api: DockviewApi, restored: boolean) => void;
		/** Where this workbench's layout lives. Omit for an ephemeral dock. */
		store?: LayoutStore;
		/**
		 * A layout already read on the SERVER, so the dock restores without a client round trip.
		 *
		 * This is the difference between one network hop after hydration and two. Without it the dock
		 * must download its own chunk AND then read the layout before it can paint anything but an
		 * empty grid — two SEQUENTIAL trips, and the seeded default flashes before the saved layout
		 * replaces it. Pass the result of a remote `query()` here and the first paint is already right.
		 *
		 * It is a `LayoutRead`, not a bare layout, because the three outcomes must survive the trip:
		 * `unreadable` read on the server must still disable saving on the client, or the SSR path
		 * becomes the one that quietly overwrites the user's work.
		 */
		initial?: LayoutRead;
		/**
		 * EXTRA context for panels, merged over what they already inherit.
		 *
		 * Usually unnecessary: this component captures its own context tree with `getAllContexts()` and
		 * hands it to every panel mount, so a zone sets context the ordinary Svelte way — ideally with
		 * `createContext`, which the docs prefer over raw keys for type safety — anywhere above `<Dock>`,
		 * and panels just call the matching getter. Use this only for values that have no component to
		 * live above the dock.
		 */
		context?: Map<unknown, unknown>;
		/**
		 * Which visible affordances the dock offers. Omit for all of them.
		 *
		 * dockview ships NO default header buttons — the per-tab close ✕ is the entire stock control
		 * set — so a dock with no chrome has no visible controls at all. `false` gives you that bare
		 * dock deliberately; a partial object opts out of individual pieces.
		 */
		chrome?: Partial<DockChrome> | boolean;
		/** Chrome wiring a zone must supply — today just the popout window's blank document. */
		chromeOptions?: DockChromeOptions;
		/** Merged over the rask defaults at construction. Change options later via `api.updateOptions`. */
		options?: Partial<DockviewOptions>;
		/** Trailing-edge debounce for autosave. A sash drag must be one write, not two hundred. */
		saveDebounceMs?: number;
		class?: string;
	}

	let {
		panels,
		onready,
		store,
		initial,
		chrome,
		chromeOptions = {},
		context,
		options,
		saveDebounceMs = 1000,
		class: className = '',
	}: Props = $props();

	let host: HTMLDivElement | null = $state(null);

	// Captured at component INIT — `getAllContexts` must be called synchronously during initialisation,
	// so it cannot move inside onMount. This is what makes a panel behave like it was rendered here:
	// `mount()` starts a NEW context tree rooted at nothing, so without this a panel calling
	// `getContext`/a `createContext` getter would find an empty tree, and every zone would be forced to
	// re-declare its state through the `context` prop with hand-rolled keys.
	const inheritedContext = getAllContexts();

	// Resolved once at init, not derived: dockview reads these at construction, and a later change
	// would have to go through api.updateOptions rather than silently disagree with what is rendered.
	const active = untrack(() => resolveChrome(chrome));
	/** The close button is unconditional, so the cluster is worth rendering if ANY control is on. */
	const hasHeaderActions =
		active.split ||
		active.addPanel ||
		active.maximize ||
		active.float ||
		active.popout ||
		active.keepEmptyGroups;

	onMount(() => {
		// onMount, NOT {@attach}. An attachment re-runs whenever a value it read changes, and re-running
		// this factory would DESTROY and rebuild the dock — every panel unmounted, every subscription
		// dropped, the layout reset — on something as small as an options tweak. The dock is a
		// long-lived instance reconciled through its own api, which is exactly the case onMount is for.
		if (host === null) return;

		// The host's own context tree, plus any explicit extras. Built once and shared by every panel
		// mount, so `getContext` resolves the same instances a component would have inherited had it
		// been rendered in this subtree.
		const panelContext =
			context === undefined
				? inheritedContext
				: new Map<unknown, unknown>([...inheritedContext, ...context]);

		const dock = new DockviewComponent(host, {
			theme: raskDockTheme,
			// Pointer events drive EVERY input type, rather than dockview's default of HTML5 drag for
			// mouse and pointer only for touch/pen. Three reasons, in order of weight:
			//   * HTML5 drag-and-drop is unreliable on Linux browsers and embedded webviews — dockview's
			//     own option docs name that as the case for this mode, and the estate runs on Linux.
			//   * It makes the dock drivable by Playwright, which dispatches pointer events natively and
			//     cannot drive HTML5 DnD. A layout engine whose central interaction is untestable in CI
			//     is one refactor away from silently breaking.
			//   * Touch and pen get the same behaviour as mouse instead of a second code path.
			// THE COST, stated precisely now that popout ships. Pointer mode loses cross-WINDOW HTML5
			// drag and the native drag image. That does NOT disable popout: `addPopoutGroup` is an api
			// call, so the header button and the context-menu row both work, and a popped-out group
			// docks back the same way. What is lost is DRAGGING a tab from the main window into an
			// already-open popout window — a gesture with no discoverable affordance anyway, against
			// a mode that makes touch, pen and Linux mouse drags all work and the dock testable in CI.
			// Set `options={{ dndStrategy: 'auto' }}` per dock if cross-window drag matters more.
			dndStrategy: 'pointer',
			// The whole-layout edge drop zone is ON in dockview by default — its live target is just
			// TEN CSS PIXELS wide, which is why "split to any direction" and "snap" read as missing
			// features rather than as an aiming problem. Widened so it can actually be hit.
			...(active.edgeDrop ? { dndEdges: EDGE_DROP } : {}),
			// Keep an emptied group alive so its header chrome survives the last tab closing. Under
			// dockview's default the group is destroyed with its final panel, taking every button with
			// it, and only the watermark can offer a way back.
			...(active.keepEmptyGroups ? { noPanelsOverlay: 'emptyGroup' as const } : {}),
			// Ctrl+] / Ctrl+[ cycle tabs, F6 / Shift+F6 walk groups, Ctrl+Shift+\ reaches the tab strip.
			// NOT Ctrl+M docking: it is implemented in the 7.0.4 bundle but its keybindings are absent
			// from DockviewKeybindings, so it cannot fire — see chrome.ts.
			...(active.keyboard ? { keyboardNavigation: true } : {}),
			...(active.contextMenu
				? { getTabContextMenuItems: makeTabContextMenu(active, chromeOptions) }
				: {}),
			// Panels live in dockview's OverlayRenderContainer and are REPOSITIONED rather than
			// re-parented. This is the difference between two kinds of state that are easy to conflate:
			//
			//   * COMPONENT state — a running interval, a Svelte Flow viewport in `$state.raw` — survives
			//     a move under either renderer, because `SveltePanelRenderer` mounts once (see its
			//     invariant) and dockview never disposes a panel it is only moving.
			//   * DOM-HELD state — scrollTop, a focused input's selection, a <video>'s playback position,
			//     an open <details> — does NOT. Under the default 'onlyWhenVisible' dockview REMOVES the
			//     panel element from the document and re-inserts it, and the browser drops all of that on
			//     detach. No remount is involved, which is exactly why it is easy to miss.
			//
			// Measured, not assumed: with the default, a run list scrolled to 260px came back at 0 after a
			// drag between groups while the panel's own counter ticked straight through it.
			//
			// The cost is that hidden panels stay rendered and keep their subscriptions live, which for a
			// WORKBENCH is the point — a background panel that stops updating is the bug docking exists to
			// avoid. A consumer with dozens of heavy panels can override this per-dock.
			defaultRenderer: 'always',
			...options,
			// The group control cluster — split / add / float / popout / maximize / close. The `right`
			// slot is the classic VS Code position and the only place persistent group chrome can live.
			// `panels` goes through because `GroupActions` is mounted with no context map, so the picker
			// cannot reach the catalogue any other way.
			...(hasHeaderActions
				? { createRightHeaderActionComponent: makeHeaderActions(active, chromeOptions, panels) }
				: {}),
			createComponent: ({ name }) => {
				const entry = panels[name];
				// An unknown name is a stale saved layout, not a bug to crash on — see MissingPanelRenderer.
				return entry
					? new SveltePanelRenderer(entry.component, panelContext)
					: new MissingPanelRenderer(name);
			},
		});

		const autosave = store ? new LayoutAutosave(store, dock.api, saveDebounceMs) : null;
		let disposed = false;

		void (async () => {
			const restored = autosave ? await autosave.restore(initial) : false;
			// The await above yields, so the component may already have been destroyed underneath us.
			if (disposed) return;
			onready?.(dock.api, restored);
			// Started only after restore + the consumer's default seeding, so neither writes back the
			// layout it just read.
			autosave?.start();
		})();

		return () => {
			disposed = true;
			autosave?.dispose();
			dock.dispose();
		};
	});
</script>

<div bind:this={host} class="rask-dock {className}"></div>
