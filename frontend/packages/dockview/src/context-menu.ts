/**
 * The tab right-click menu.
 *
 * Omit `getTabContextMenuItems` and dockview shows NO menu — the native browser menu passes through,
 * which is exactly the state rask shipped in. dockview positions, styles and dismisses the popover
 * itself, so this is the cheapest possible home for the long tail of actions: no portal, no focus
 * trap, no z-index fight with the grid.
 *
 * Deliberately built from plain `label` + `action` entries rather than the framework-component hatch
 * (`createContextMenuItemComponent`). A Svelte component per row would mean a mount/dispose lifecycle
 * inside a popover dockview destroys on every dismiss, for rows that are a word and a click handler.
 * The framework hatch is there if a row ever needs a colour swatch or a sparkline.
 *
 * Only FOUR built-in strings exist in 7.0.4 — `'close'`, `'closeOthers'`, `'closeAll'`, `'separator'`.
 * The upstream docs also list `closeLeft`, `closeRight` and `maximize`; those are master-branch drift
 * and are not in this release, so they are spelled out as custom rows instead of trusted as built-ins.
 */
import type { ContextMenuItem, GetTabContextMenuItemsParams } from 'dockview';
import type { DockChrome, DockChromeOptions } from './chrome';
import { splitPanel, splitVerb } from './split';

export function makeTabContextMenu(
	chrome: DockChrome,
	options: DockChromeOptions,
): (params: GetTabContextMenuItemsParams) => ContextMenuItem[] {
	return ({ panel, group, api }) => {
		const items: ContextMenuItem[] = ['close', 'closeOthers', 'closeAll'];
		const location = group.api.location;
		const kind = typeof location === 'string' ? location : location.type;
		const inGrid = kind === 'grid';

		// Mirrors the header buttons: with one panel `moveTo` is a documented no-op, so the row
		// duplicates instead. Never disabled — every zone seeds one-panel groups.
		if (chrome.split && inGrid) {
			const verb = splitVerb(group);
			items.push('separator');
			for (const [side, position] of [
				['right', 'right'],
				['left', 'left'],
				['down', 'bottom'],
				['up', 'top'],
			] as const) {
				items.push({
					label: `${verb} ${side}`,
					action: () => splitPanel(api, group, panel, position),
				});
			}
		}

		const locationItems: ContextMenuItem[] = [];
		if (chrome.float && kind !== 'popout') {
			locationItems.push(
				kind === 'floating'
					? {
							label: 'Dock back into the layout',
							action: () => group.api.moveTo({ position: 'center' }),
						}
					: { label: 'Float above the layout', action: () => api.addFloatingGroup(panel) },
			);
		}
		if (chrome.popout && options.popoutUrl !== undefined && kind !== 'popout') {
			locationItems.push({
				label: 'Open in a new window',
				// Fire-and-forget: addPopoutGroup resolves false when the browser blocks the window, and a
				// context-menu row has no surface to report that on — the header button does.
				action: () => void api.addPopoutGroup(panel, { popoutUrl: options.popoutUrl }),
			});
		}
		if (chrome.maximize && inGrid) {
			locationItems.push({
				label: group.api.isMaximized() ? 'Restore' : 'Maximize this group',
				action: () => (group.api.isMaximized() ? group.api.exitMaximized() : group.api.maximize()),
			});
		}

		if (locationItems.length > 0) items.push('separator', ...locationItems);
		return items;
	};
}
