/**
 * The dock's CHROME — the affordances a user can see and click.
 *
 * The layout API stays deliberately thin: a consumer holds the real `DockviewApi`. Chrome is the
 * opposite, and on purpose. dockview ships **no** default header buttons at all — the per-tab close
 * ✕ is genuinely the only control that exists out of the box — so a thin binding leaves every zone
 * hand-writing three renderer factories, the split maths, and a context-menu array. Three zones
 * doing that independently is three drifting answers to one question.
 *
 * So chrome is an ABSTRACTION with working defaults: a zone declares intent, this package owns every
 * `createRightHeaderActionComponent`, `moveTo({ position })` and `getTabContextMenuItems` call.
 *
 *     <Dock {panels} />                              // everything on
 *     <Dock {panels} chrome={{ popout: false }} />   // opt out of one thing
 *     <Dock {panels} chrome={false} />               // bare dock, wire it yourself
 *
 * WHAT WAS ALREADY ON AND INVISIBLE. Three of the features that read as "missing" were enabled all
 * along and simply undiscoverable, which is worse than absent — the capability is paid for in bytes
 * and nobody can reach it:
 *
 *   - `dndEdges` (drop against the whole layout's outer border) is ON by default, but its live
 *     target is **10 CSS pixels** wide. {@link DEFAULT_CHROME} widens it.
 *   - Every group body already resolves a five-way drop (four edge quadrants + centre). Users who
 *     only ever drop onto the tab strip never meet it.
 *   - Floating already works via `shift`+drag on a tab. No user discovers a modifier chord.
 *
 * NOT AVAILABLE, whatever the docs suggest — all upstream-paid, none in the MIT core:
 * smart alignment guides, the five-zone drop compass, layout undo/redo, pinned tabs, auto-hiding
 * edge groups. And `Ctrl+M` two-phase keyboard docking is fully implemented in the 7.0.4 bundle but
 * its keybindings are **not declared on `DockviewKeybindings`**, so it cannot fire — wiring it from
 * the docs would silently do nothing.
 */
import type { DroptargetOverlayModel } from 'dockview';

export interface DockChrome {
	/** Split the active panel out of its group, up/down/left/right. Header button + context menu. */
	split: boolean;
	/**
	 * The `+` control: add a registered panel to this group, chosen from a searchable list.
	 *
	 * A DIFFERENT control from {@link split}, and both are wanted. Split acts on the PANE — divide this
	 * space in a direction. `+` acts on the CONTENT — put a named panel here. Conflating them is what
	 * produced a split button whose only possible outcome was a second copy of the panel you were
	 * already looking at.
	 */
	addPanel: boolean;
	/** Expand one group to fill the dock; the button flips to restore while maximized. */
	maximize: boolean;
	/** Lift a group into a draggable overlay card above the grid. */
	float: boolean;
	/**
	 * Open a group in a real second browser window.
	 *
	 * Requires the consuming zone to serve {@link DockChromeOptions.popoutUrl} — a blank document.
	 * Left ON because the button self-disables when no url is configured, which is a visible,
	 * explicable state rather than a silent no-op.
	 */
	popout: boolean;
	/** Right-click a tab: Close / Close Others / Close All, plus the split and location actions. */
	contextMenu: boolean;
	/**
	 * Ctrl+] / Ctrl+[ cycle tabs, F6 / Shift+F6 walk groups, Ctrl+Shift+\ jumps to the tab strip.
	 * NOT Ctrl+M docking — see the module docstring.
	 */
	keyboard: boolean;
	/** Keep an emptied group (and its header chrome) alive instead of destroying it. */
	keepEmptyGroups: boolean;
	/** Widen the whole-layout edge drop zone so it is findable. See {@link EDGE_DROP}. */
	edgeDrop: boolean;
}

export interface DockChromeOptions {
	/**
	 * A blank document for popout windows to load, e.g. `${base}/popout.html`.
	 *
	 * Zone-shaped, so it cannot be defaulted here: each zone is served under its own base path. The
	 * parent document's stylesheets ARE copied into the popout, so rask's OKLCH tokens and Tailwind
	 * utilities resolve there with no extra work — the file itself only needs an empty body.
	 */
	popoutUrl?: string;
}

/**
 * dockview's own default edge target is `activationSize: 10px`, which is why nobody finds it.
 *
 * 48px is a deliberate, modest widening — roughly a finger-width, hittable by aiming at the border
 * rather than by accident. It was first written as 15% of the smaller axis, which on a 1244×808 dock
 * is a **121px band around every outer edge** and reaches well inside the tab strips of top-row
 * groups. That is a percentage doing something a pixel value should: the tab strip is ~36px whatever
 * the viewport, so the safe margin is absolute, not relative.
 *
 * ⚠️ Not verified by automation. The header buttons and the tab context menu are the affordances the
 * discoverability fix actually rests on; this only widens a target that was already there.
 */
export const EDGE_DROP: DroptargetOverlayModel = {
	size: { type: 'pixels', value: 30 },
	activationSize: { type: 'pixels', value: 48 },
};

/** Everything on. A workbench with no visible controls is the defect this package exists to avoid. */
export const DEFAULT_CHROME: DockChrome = {
	split: true,
	addPanel: true,
	maximize: true,
	float: true,
	popout: true,
	contextMenu: true,
	keyboard: true,
	keepEmptyGroups: true,
	edgeDrop: true,
};

/** Normalise the prop: `undefined` = all on, `false` = none, a partial = defaults with overrides. */
export function resolveChrome(chrome: Partial<DockChrome> | boolean | undefined): DockChrome {
	if (chrome === false) {
		return {
			split: false,
			addPanel: false,
			maximize: false,
			float: false,
			popout: false,
			contextMenu: false,
			keyboard: false,
			keepEmptyGroups: false,
			edgeDrop: false,
		};
	}
	if (chrome === true || chrome === undefined) return DEFAULT_CHROME;
	return { ...DEFAULT_CHROME, ...chrome };
}
