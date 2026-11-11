<script lang="ts">
	import { ChevronRight } from '@lucide/svelte';
	import * as Collapsible from '../components/collapsible/index.js';
	import * as Sidebar from '../components/sidebar/index.js';
	import { useSidebar } from '../components/sidebar/context.svelte.js';
	import type { PageNav, PageNavLeaf } from './nav-config.js';

	// The console's routes, in sections, all soft navigations inside one router.
	let { pathname = '', nav = null }: { pathname?: string; nav?: PageNav | null } = $props();

	// The RAIL's own collapse state. Load-bearing for `groupOpen` below: in icon mode a collapsed
	// group renders NOTHING — `Collapsible.Content` does not mount its items — so the icons vanish,
	// and the label you would click to get them back is itself hidden by `group-data-[collapsible=icon]`.
	// The rail would then show a gap where a section is, with no way to recover it.
	const sidebar = useSidebar();

	/** A group is open when it is not explicitly collapsed, OR when it holds the active route — a
	 *  section that hides the page you are on reads as a broken sidebar — OR whenever the rail is
	 *  collapsed to icons, where "closed" would mean the section's icons are simply absent. */
	function groupOpen(items: PageNavLeaf[], collapsed: boolean | undefined, p: string): boolean {
		if (sidebar.state === 'collapsed') return true;
		return !collapsed || items.some((i) => isActive(i, p));
	}

	/** Active if the leaf matches, or any of its children do (so a parent highlights and expands
	 *  while you are inside it). */
	function isActive(leaf: PageNavLeaf, p: string): boolean {
		return leaf.match(p) || (leaf.children?.some((c) => c.match(p)) ?? false);
	}
</script>

{#snippet leafLink(leaf: PageNavLeaf, props: Record<string, unknown>)}
	{#if leaf.unavailable}
		<!-- SHOWN, not hidden — and not a link. A <span> rather than a disabled <a>: an anchor with an
		     href is reachable by keyboard and by paste however it is styled, and this must not navigate
		     to a page that cannot render. `aria-disabled` and the reason as its title carry it to a
		     screen reader, which `pointer-events-none` alone would not. -->
		<span
			{...props}
			aria-disabled="true"
			title="{leaf.title} — {leaf.unavailable}"
			class="cursor-not-allowed opacity-50 {(props['class'] as string) ?? ''}"
		>
			{#if leaf.icon}
				<leaf.icon />
			{/if}
			<span>{leaf.title}</span>
		</span>
	{:else}
		<a href={leaf.href} {...props}>
			{#if leaf.icon}
				<leaf.icon />
			{/if}
			<span>{leaf.title}</span>
		</a>
	{/if}
{/snippet}

{#snippet rows(items: PageNavLeaf[])}
	<Sidebar.Menu>
		{#each items as leaf (leaf.title)}
			<Sidebar.MenuItem>
				<Sidebar.MenuButton tooltipContent={leaf.title} isActive={isActive(leaf, pathname)}>
					{#snippet child({ props })}
						{@render leafLink(leaf, props)}
					{/snippet}
				</Sidebar.MenuButton>
				{#if leaf.children?.length && !leaf.unavailable}
					<!-- Sub-routes stay mounted but collapse with the parent, so the rail keeps a stable
					     height while you move around inside a section. -->
					<Collapsible.Root open={isActive(leaf, pathname)}>
						<Collapsible.Content>
							<Sidebar.MenuSub>
								{#each leaf.children as sub (sub.title)}
									<Sidebar.MenuSubItem>
										<Sidebar.MenuSubButton isActive={sub.match(pathname)}>
											{#snippet child({ props })}
												{@render leafLink(sub, props)}
											{/snippet}
										</Sidebar.MenuSubButton>
									</Sidebar.MenuSubItem>
								{/each}
							</Sidebar.MenuSub>
						</Collapsible.Content>
					</Collapsible.Root>
				{/if}
			</Sidebar.MenuItem>
		{/each}
	</Sidebar.Menu>
{/snippet}

{#if nav}
	{#each nav.groups as group, index (group.label ?? `rows-${index}`)}
		{#if group.label === undefined}
			<!-- Rows with no heading: the top of the rail, above every labelled section. -->
			<Sidebar.Group>
				<Sidebar.GroupContent>
					{@render rows(group.items)}
				</Sidebar.GroupContent>
			</Sidebar.Group>
		{:else}
			{@const open = groupOpen(group.items, group.defaultCollapsed, pathname)}
			<Collapsible.Root {open} class="group/collapsible">
				<Sidebar.Group>
					<Sidebar.GroupLabel>
						{#snippet child({ props })}
							<Collapsible.Trigger {...props}>
								{group.label}
								<ChevronRight
									class="ms-auto transition-transform group-data-[state=open]/collapsible:rotate-90"
								/>
							</Collapsible.Trigger>
						{/snippet}
					</Sidebar.GroupLabel>
					<Collapsible.Content>
						<Sidebar.GroupContent>
							{@render rows(group.items)}
						</Sidebar.GroupContent>
					</Collapsible.Content>
				</Sidebar.Group>
			</Collapsible.Root>
		{/if}
	{/each}
{/if}
