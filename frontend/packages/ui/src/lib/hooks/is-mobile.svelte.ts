import { MediaQuery } from 'svelte/reactivity';

/**
 * The width at which the shared shell folds: the sidebar becomes an off-canvas sheet and the top
 * navbar's zone links collapse into a single overflow menu. One constant, because the two MUST
 * flip together — a bar that still lays out horizontally while the sidebar has already gone
 * off-canvas is exactly how the zone links ended up running under the account avatar.
 *
 * 1024 (tailwind's `lg`) rather than 768: at 900px the estate row is sidebar + project switcher +
 * three zone triggers + the account control, and a project label anywhere near the switcher's
 * `max-w-48` leaves the last trigger overlapping the avatar. The sidebar's own desktop-branch
 * media classes are `lg:` for the same reason — they must agree with this number.
 */
export const SHELL_COLLAPSE_BREAKPOINT = 1024;

const DEFAULT_MOBILE_BREAKPOINT = 768;

export class IsMobile extends MediaQuery {
	constructor(breakpoint: number = DEFAULT_MOBILE_BREAKPOINT) {
		super(`max-width: ${breakpoint - 1}px`);
	}
}
