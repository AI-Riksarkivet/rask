/**
 * Mounts {@link GroupActions} into a group's header slot.
 *
 * Same shape as `SveltePanelRenderer` and for the same reason: mount once in `init()`, unmount once
 * in `dispose()`. A header renderer is created per GROUP rather than per panel, and dockview disposes
 * it when the group goes — so a group dragged around the layout keeps this instance, and the buttons
 * keep their subscriptions.
 *
 * `IGroupHeaderProps` carries exactly three fields (`api`, `containerApi`, `group`) and core builds
 * no reactive props object for a framework — that is the adapter's job, which is why GroupActions
 * subscribes to `onDidLocationChange` / `onDidMaximizedGroupChange` itself.
 */
import { mount, unmount } from 'svelte';
import type { DockviewGroupPanel, IGroupHeaderProps } from 'dockview';
import GroupActions from './GroupActions.svelte';
import type { DockChrome, DockChromeOptions } from './chrome';
import type { PanelRegistry } from './types';

/** dockview's own contract for a header slot; declared structurally to avoid a value import. */
interface HeaderActionsRenderer {
	readonly element: HTMLElement;
	init(params: IGroupHeaderProps): void;
	dispose(): void;
}

export function makeHeaderActions(
	chrome: DockChrome,
	options: DockChromeOptions,
	panels: PanelRegistry,
): (group: DockviewGroupPanel) => HeaderActionsRenderer {
	return () => new SvelteHeaderActions(chrome, options, panels);
}

class SvelteHeaderActions implements HeaderActionsRenderer {
	readonly element: HTMLElement;

	#chrome: DockChrome;
	#options: DockChromeOptions;
	/**
	 * The zone's catalogue, so the `+` picker can list it.
	 *
	 * Handed down the constructor rather than reached for: this renderer is built by dockview outside
	 * any Svelte tree, and `init()` below mounts `GroupActions` with NO context map at all — so a
	 * context getter would find an empty tree here, however well it works for panel bodies.
	 */
	#panels: PanelRegistry;
	#instance: Record<string, unknown> | null = null;

	constructor(chrome: DockChrome, options: DockChromeOptions, panels: PanelRegistry) {
		this.#chrome = chrome;
		this.#options = options;
		this.#panels = panels;
		this.element = document.createElement('div');
		// `position: relative` anchors the popout-blocked note; the class is styled in styles.css.
		this.element.className = 'rask-dock-actions-host';
	}

	init(params: IGroupHeaderProps): void {
		this.#instance = mount(GroupActions, {
			target: this.element,
			props: {
				api: params.api,
				containerApi: params.containerApi,
				group: params.group,
				chrome: this.#chrome,
				options: this.#options,
				panels: this.#panels,
			},
		}) as Record<string, unknown>;
	}

	dispose(): void {
		if (this.#instance !== null) {
			void unmount(this.#instance);
			this.#instance = null;
		}
	}
}
