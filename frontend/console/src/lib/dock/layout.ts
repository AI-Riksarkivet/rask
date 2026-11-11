/**
 * The workbench's layout: which panels exist, the arrangement a first visit opens with, and the
 * edits the dock's own controls make to a `LayoutConfig` (close a panel, open one, read a stored one).
 *
 * Pure functions over plain objects, so the arrangement a person saves is the arrangement they get
 * back: `horizon-layout` takes the whole layout as ONE bindable value, and everything here returns a
 * new value rather than reaching into the library's state.
 */
import {
	parseLayoutConfig,
	validateConfig,
	type Id,
	type LayoutConfig,
	type NodeConfig,
	type SplitConfig,
	type TabGroupConfig,
} from 'horizon-layout';

/** Every panel the workbench can show, in the order the panel picker lists them. */
export const PANELS = [
	{ id: 'tables', title: 'Tables', keywords: ['tables', 'catalog', 'registry', 'namespace'] },
	{ id: 'events', title: 'Events', keywords: ['events', 'feed', 'lineage', 'log'] },
] as const satisfies readonly { id: string; title: string; keywords: readonly string[] }[];

export type PanelId = (typeof PANELS)[number]['id'];

const PANEL_IDS: ReadonlySet<string> = new Set(PANELS.map((p) => p.id));

export function isPanelId(id: string): id is PanelId {
	return PANEL_IDS.has(id);
}

/** The arrangement a first visit opens with: Tables beside Events, half and half. */
export function defaultLayout(): LayoutConfig {
	return {
		root: {
			direction: 'horizontal',
			views: [
				{ tabs: ['tables'], activeTabIndex: 0 },
				{ tabs: ['events'], activeTabIndex: 0 },
			],
			splitPoints: [0.5],
		},
	};
}

const isSplit = (node: NodeConfig): node is SplitConfig => 'splitPoints' in node;

function tabsOf(node: NodeConfig | undefined): Id[] {
	if (node === undefined) return [];
	return isSplit(node) ? node.views.flatMap((v) => tabsOf(v)) : [...node.tabs];
}

/** Every panel on screen: docked in a group, popped out, or maximized. */
export function openPanels(config: LayoutConfig): Set<Id> {
	return new Set([...tabsOf(config.root), ...(config.popouts ?? [])]);
}

/** The panels the picker offers: every known panel not on screen. */
export function closedPanels(config: LayoutConfig): PanelId[] {
	const open = openPanels(config);
	return PANELS.map((p) => p.id).filter((id) => !open.has(id));
}

/** `node` without `id`, or undefined when nothing is left of it; a split left with one child is
 *  replaced by that child, as the library does when a drag empties a group. */
function withoutTab(node: NodeConfig, id: Id): NodeConfig | undefined {
	if (!isSplit(node)) {
		const index = node.tabs.indexOf(id);
		if (index === -1) return node;
		const tabs = node.tabs.filter((t) => t !== id);
		if (tabs.length === 0) return undefined;
		const active = node.activeTabIndex > index ? node.activeTabIndex - 1 : node.activeTabIndex;
		return {
			tabs: tabs as TabGroupConfig['tabs'],
			activeTabIndex: Math.min(active, tabs.length - 1),
		};
	}
	const kept: { node: NodeConfig; size: number }[] = [];
	const bounds = [0, ...node.splitPoints, 1];
	node.views.forEach((child, i) => {
		const next = withoutTab(child, id);
		if (next !== undefined) kept.push({ node: next, size: bounds[i + 1]! - bounds[i]! });
	});
	if (kept.length === 0) return undefined;
	if (kept.length === 1) return kept[0]!.node;
	// The removed pane's share is spread over the rest in proportion, so the survivors keep their
	// relative sizes.
	const total = kept.reduce((sum, k) => sum + k.size, 0);
	const points: number[] = [];
	let edge = 0;
	for (const k of kept.slice(0, -1)) {
		edge += k.size / total;
		points.push(Number(edge.toFixed(4)));
	}
	return {
		direction: node.direction,
		views: kept.map((k) => k.node) as SplitConfig['views'],
		splitPoints: points as SplitConfig['splitPoints'],
	};
}

/** Close a panel wherever it is: docked, popped out or maximized. */
export function closePanel(config: LayoutConfig, id: Id): LayoutConfig {
	const root = config.root === undefined ? undefined : withoutTab(config.root, id);
	const popouts = (config.popouts ?? []).filter((p) => p !== id);
	return {
		...(root ? { root } : {}),
		...(config.maximizedView && config.maximizedView !== id
			? { maximizedView: config.maximizedView }
			: {}),
		...(popouts.length > 0 ? { popouts } : {}),
	};
}

/** The tab group holding `id`, or the first group when none does. */
function groupOf(node: NodeConfig, id: Id | undefined): TabGroupConfig | undefined {
	if (!isSplit(node)) return id === undefined || node.tabs.includes(id) ? node : undefined;
	for (const child of node.views) {
		const found = groupOf(child, id);
		if (found) return found;
	}
	return undefined;
}

function mapGroups(node: NodeConfig, fn: (group: TabGroupConfig) => TabGroupConfig): NodeConfig {
	if (!isSplit(node)) return fn(node);
	return { ...node, views: node.views.map((v) => mapGroups(v, fn)) as SplitConfig['views'] };
}

/**
 * Open a closed panel as the active tab of the group that holds `beside` (the group whose control
 * was used), or of the first group; an empty dock gets a group of its own.
 */
export function openPanel(config: LayoutConfig, id: Id, beside?: Id): LayoutConfig {
	if (openPanels(config).has(id)) return config;
	const { root } = config;
	if (root === undefined) return { ...config, root: { tabs: [id], activeTabIndex: 0 } };
	const target = groupOf(root, beside) ?? groupOf(root, undefined);
	return {
		...config,
		root: mapGroups(root, (group) =>
			group === target
				? { tabs: [...group.tabs, id] as TabGroupConfig['tabs'], activeTabIndex: group.tabs.length }
				: group,
		),
	};
}

/** Pop a docked panel out into a window of its own. */
export function popOut(config: LayoutConfig, id: Id): LayoutConfig {
	const docked = closePanel(config, id);
	return { ...docked, popouts: [...(docked.popouts ?? []), id] };
}

/** The three outcomes of reading a stored layout. `unreadable` means one exists and could not be
 *  read: it is reported and never replaced by the default, so no save overwrites it. */
export type LayoutRead =
	| { readonly status: 'ok'; readonly layout: LayoutConfig }
	| { readonly status: 'absent' }
	| { readonly status: 'unreadable'; readonly detail: string };

const KNOWN: ReadonlyMap<Id, true> = new Map(PANELS.map((p) => [p.id, true] as const));

/** A stored value as a layout this workbench can show: parsed, then validated against its panels. */
export function readStoredLayout(raw: unknown): LayoutRead {
	try {
		const layout = parseLayoutConfig(raw);
		validateConfig(layout, KNOWN);
		// A popout's window closes with the page that opened it, so a stored popout is docked again:
		// left in `popouts`, it would count as open with no window to show it in.
		let docked: LayoutConfig = { ...layout };
		delete docked.popouts;
		for (const id of layout.popouts ?? []) docked = openPanel(docked, id);
		return { status: 'ok', layout: docked };
	} catch (e) {
		return {
			status: 'unreadable',
			detail: `the stored layout is not one this workbench can show: ${e instanceof Error ? e.message : String(e)}`,
		};
	}
}
