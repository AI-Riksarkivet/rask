<script lang="ts">
	import { ChevronRight } from '@lucide/svelte';
	import * as Collapsible from '../components/collapsible/index.js';
	import * as Sidebar from '../components/sidebar/index.js';
	import { prefetchOnIntent, type ZoneNav, type ZoneNavLeaf } from './nav-config.js';

	// The CURRENT zone's own routes, as LABELLED GROUPS. The cross-zone list lives in the top navbar
	// (`TopNavbar`), so leaves here are same-zone by construction and stay SOFT navs (no
	// data-sveltekit-reload — it would defeat the SPA router inside the zone) EXCEPT a leaf that
	// declares `reload`: that one points outside this zone's route manifest (e.g. media's Annotate →
	// /annotator) and must hard-navigate, so it also warms the target document on intent like the
	// navbar's cross-zone entries.
	//
	// This replaced a single flat list. Lakehouse had to compensate by swapping the entire sidebar per
	// area, which meant the zone's other four areas were invisible from wherever you stood.
	let { pathname = '', nav = null }: { pathname?: string; nav?: ZoneNav | null } = $props();

	/** A group is open when it is not explicitly collapsed, OR when it holds the active route — a
	 *  section that hides the page you are on reads as a broken sidebar. */
	function groupOpen(items: ZoneNavLeaf[], collapsed: boolean | undefined, p: string): boolean {
		return !collapsed || items.some((i) => isActive(i, p));
	}

	/** Active if the leaf matches, or any of its children do (so a parent highlights and expands
	 *  while you are inside it). */
	function isActive(leaf: ZoneNavLeaf, p: string): boolean {
		return leaf.match(p) || (leaf.children?.some((c) => c.match(p)) ?? false);
	}
</script>

{#snippet leafLink(leaf: ZoneNavLeaf, props: Record<string, unknown>)}
	<a
		href={leaf.href}
		data-sveltekit-reload={leaf.reload ? '' : undefined}
		{...props}
		{@attach (el) => (leaf.reload ? prefetchOnIntent(leaf.href)(el) : undefined)}
	>
		{#if leaf.icon}
			<leaf.icon />
		{/if}
		<span>{leaf.title}</span>
	</a>
{/snippet}

{#if nav}
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
