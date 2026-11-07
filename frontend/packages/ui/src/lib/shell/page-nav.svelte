<script lang="ts">
	import { ChevronRight } from '@lucide/svelte';
	import * as Collapsible from '../components/collapsible/index.js';
	import * as Sidebar from '../components/sidebar/index.js';
	import { useSidebar } from '../components/sidebar/context.svelte.js';
	import type { PageNav, PageNavLeaf } from './nav-config.js';

	// The app's own routes, as LABELLED GROUPS, all soft navigations inside one router.
	let { pathname = '', nav = null }: { pathname?: string; nav?: PageNav | null } = $props();

	// The RAIL's own collapse state. Load-bearing for `groupOpen` below: in icon mode a collapsed
	// group renders NOTHING — `Collapsible.Content` does not mount its items — so the icons vanish,
	// and the label you would click to get them back is itself hidden by `group-data-[collapsible=icon]`.
	// The rail then shows a gap where a section used to be, with no way to recover it.
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
		<!-- SHOWN, not hidden — and not a link. Dropping the row reads as "this product has no
		     knowledge graph"; greying it says "THIS corpus has none", which is the true statement and
		     the one that tells you what to change. A <span> rather than a disabled <a>: an anchor with
		     href is reachable by keyboard and by paste no matter how it is styled, and this must not
		     navigate to a page that cannot render. `aria-disabled` + the reason as the tooltip carry
		     it to a screen reader, which `pointer-events-none` alone would not. -->
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

{#if nav}
	{#if nav.root}
		<!-- The app ROOT, above and outside every group: it is the landing that summarises the
		     sections, not a member of one. No GroupLabel — a one-row labelled section is noise. -->
		<Sidebar.Group>
			<Sidebar.GroupContent>
				<Sidebar.Menu>
					<Sidebar.MenuItem>
						<Sidebar.MenuButton
							tooltipContent={nav.root.title}
							isActive={isActive(nav.root, pathname)}
						>
							{#snippet child({ props })}
								{@render leafLink(nav.root!, props)}
							{/snippet}
						</Sidebar.MenuButton>
					</Sidebar.MenuItem>
				</Sidebar.Menu>
			</Sidebar.GroupContent>
		</Sidebar.Group>
	{/if}
	{#each nav.groups as group (group.label)}
		{@const open = groupOpen(group.items, group.defaultCollapsed, pathname)}
		<Collapsible.Root {open} class="group/collapsible">
			<Sidebar.Group>
				{#if group.collapsible === false}
					<Sidebar.GroupLabel>{group.label}</Sidebar.GroupLabel>
				{:else}
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
				{/if}
				<Collapsible.Content>
					<Sidebar.GroupContent>
						<Sidebar.Menu>
							{#each group.items as leaf (leaf.title)}
								<Sidebar.MenuItem>
									<Sidebar.MenuButton tooltipContent={leaf.title} isActive={leaf.match(pathname)}>
										{#snippet child({ props })}
											{@render leafLink(leaf, props)}
										{/snippet}
									</Sidebar.MenuButton>
									{#if leaf.children?.length}
										<!-- Sub-routes stay mounted but collapse with the parent, so the rail keeps
										     a stable height while you move around inside a section. -->
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
					</Sidebar.GroupContent>
				</Collapsible.Content>
			</Sidebar.Group>
		</Collapsible.Root>
	{/each}
{/if}
