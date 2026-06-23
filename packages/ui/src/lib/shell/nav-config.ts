import { Server, FileText, Database, LayoutGrid, GraduationCap, Shapes } from '@lucide/svelte';

/** All lucide icons share one component signature, so any icon's type fits. */
type IconComponent = typeof Server;

/** A leaf route inside a collapsible domain item. */
export type NavLeaf = {
	title: string;
	/** ABSOLUTE, project-prefixed href (e.g. /default/compute/cluster). */
	href: string;
	/** Active predicate vs the FULL pathname. */
	match: (p: string) => boolean;
};

/** A top-level sidebar item — a domain. With `items` it renders as a collapsible
 *  accordion (sidebar-07 NavMain); without, as a direct link. */
export type NavItem = {
	title: string;
	icon: IconComponent;
	href: string;
	match: (p: string) => boolean;
	items?: NavLeaf[];
};

/** A selectable project — the sidebar header switcher. Backend support is pending;
 *  there is one implicit "default" project today. */
export type Project = { name: string; subtitle?: string };

/** The footer profile identity. No auth yet, so this is a local placeholder. */
export type NavUser = { name: string; email?: string; initials?: string };

/** prefix-segment match: active when the path equals the href or is nested under it. */
const seg = (href: string) => (p: string) => p === href || p.startsWith(href + '/');
/** domain match: active when the path is under any of the given prefixes. */
const under =
	(...prefixes: string[]) =>
	(p: string) =>
		prefixes.some((pre) => p === pre || p.startsWith(pre + '/'));

/**
 * Project-scoped sidebar nav (project-first IA). The sidebar is only shown INSIDE a
 * project, so every href is prefixed with `/<project>` and `match` is evaluated against
 * the full pathname. **Overview** (the project landing — the batch view) is first; there
 * is no "Home" item here — Home is the pre-project landing at `/` (the project picker),
 * which renders WITHOUT this sidebar.
 *
 * It's a factory, not a const: the active project comes from the URL's first segment, so
 * the same nav renders correctly for any `/<project>/…`.
 */
export function navMain(project: string): NavItem[] {
	const b = `/${project}`;
	return [
		{ title: 'Overview', icon: LayoutGrid, href: `${b}/overview`, match: seg(`${b}/overview`) },
		{
			title: 'Compute',
			icon: Server,
			href: `${b}/compute`,
			match: under(`${b}/compute`),
			items: [
				{ title: 'Cluster', href: `${b}/compute/cluster`, match: seg(`${b}/compute/cluster`) },
				{ title: 'Jobs', href: `${b}/compute/jobs`, match: seg(`${b}/compute/jobs`) },
				{ title: 'Actors', href: `${b}/compute/actors`, match: seg(`${b}/compute/actors`) },
				{ title: 'Logs', href: `${b}/compute/logviewer`, match: seg(`${b}/compute/logviewer`) },
				{ title: 'Serve', href: `${b}/compute/serve`, match: seg(`${b}/compute/serve`) },
				{ title: 'API', href: `${b}/compute/api-docs`, match: seg(`${b}/compute/api-docs`) },
			],
		},
		{
			title: 'Discover',
			icon: FileText,
			href: `${b}/discover/search`,
			match: under(`${b}/discover`),
			items: [
				{ title: 'Search', href: `${b}/discover/search`, match: seg(`${b}/discover/search`) },
				{ title: 'Viewer', href: `${b}/discover/viewer`, match: seg(`${b}/discover/viewer`) },
				{ title: 'Browse', href: `${b}/discover/browse`, match: seg(`${b}/discover/browse`) },
			],
		},
		{ title: 'Storage', icon: Database, href: `${b}/storage`, match: seg(`${b}/storage`) },
		{ title: 'Train', icon: GraduationCap, href: `${b}/train`, match: seg(`${b}/train`) },
		{ title: 'Studio', icon: Shapes, href: `${b}/studio`, match: seg(`${b}/studio`) },
	];
}
