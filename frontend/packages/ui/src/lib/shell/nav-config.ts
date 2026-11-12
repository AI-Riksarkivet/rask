import type { Database } from '@lucide/svelte';

/** All lucide icons share one component signature, so any icon's type fits. */
export type IconComponent = typeof Database;

/** One entry of the reader's activity log, as the bell renders it: a write the app sent and its
 *  outcome. Declared structurally, so the app's own store satisfies it without importing from here.
 *  A failure's `status` is the HTTP status the door answered (0: unreachable), or `null` when the
 *  write failed before any answer. */
export type ActivityEntryLike = { id: string; label: string; at: number } & (
	| { ok: true }
	| { ok: false; status: number | null; detail: string }
);

/** What the app hands the shell to light up the navbar's bell: the reader's own activity, newest
 *  first, which the app keeps. The shell never stores it; opening the panel and clearing the log come
 *  back through the callbacks. */
export type ActivityFeed = {
	entries: readonly ActivityEntryLike[];
	/** Entries recorded since the panel was last opened. */
	unread: number;
	/** The panel opened: the app marks every entry seen. */
	onopen?: () => void;
	/** The reader asked to clear the log. */
	onclear?: () => void;
};

/** A leaf route in the sidebar. */
export type PageNavLeaf = {
	title: string;
	/** ABSOLUTE, domain-relative href (e.g. /catalog/tables). */
	href: string;
	/** Active predicate vs the FULL pathname. */
	match: (p: string) => boolean;
	icon?: IconComponent;
	/** Sub-routes, rendered as a `Sidebar.MenuSub` under this leaf and auto-expanded while any of
	 *  them (or the parent) is active. One level only — a third tier is a sign the PAGE should have
	 *  been split, not the menu. */
	children?: PageNavLeaf[];
	/** WHY this area cannot be opened by the current identity — one plain sentence, or absent when it
	 *  can.
	 *
	 *  A leaf carrying this renders DISABLED rather than being dropped: a missing row reads as a
	 *  product without the feature, while a disabled one says who may open it. The reason is printed
	 *  once under its group (rows sharing it share the note) and is each row's description, so a
	 *  pointer, a keyboard and a touch all reach it; the icon rail's tooltip carries it too. It is not the door: the route's own guard refuses regardless,
	 *  and a disabled row cannot navigate. Its `children` must be dropped by whoever disables it, so
	 *  showing the row hands out none of its sub-routes. */
	unavailable?: string;
};

/** One section of the sidebar: a `Sidebar.Group`, under a collapsible label when it has one. */
export type PageNavGroup = {
	/** The section heading (e.g. "Catalog"). Absent: the rows render with no heading, as the top
	 *  rows and the pinned footer do. */
	label?: string;
	items: PageNavLeaf[];
	/** Start collapsed. Ignored while the group contains the active route. Once the reader opens or
	 *  closes the group, their choice holds; a closed group holding the active route marks its
	 *  heading. */
	defaultCollapsed?: boolean;
};

/** The sidebar config the app passes to the shared AppShell, grouped into sections so the whole
 *  surface is visible at once. */
export type PageNav = {
	groups: PageNavGroup[];
	/** PINNED TO THE BOTTOM, in `Sidebar.Footer` — it does not scroll with `groups`. Rendered by the
	 *  same `PageNav` component as the rail, so a footer leaf keeps active-state matching and its
	 *  tooltip. */
	footer?: PageNavGroup;
};

/** The navbar profile identity — populated from the OIDC session. */
export type NavUser = { name: string; email?: string; initials?: string };
