<script lang="ts">
	import { ChevronRight } from '@lucide/svelte';
	import * as Collapsible from '../components/collapsible/index.js';
	import * as Sidebar from '../components/sidebar/index.js';
	import { useSidebar } from '../components/sidebar/context.svelte.js';
	import { mergeProps } from 'bits-ui';
	import type { PageNav, PageNavGroup, PageNavLeaf } from './nav-config.js';

	// The console's routes, in sections, all soft navigations inside one router.
	let { pathname = '', nav = null }: { pathname?: string; nav?: PageNav | null } = $props();

	// The RAIL's own collapse state. Load-bearing for `groupOpen` below: in icon mode a collapsed
	// group renders NOTHING — `Collapsible.Content` does not mount its items — so the icons vanish,
	// and the label you would click to get them back is itself hidden by `group-data-[collapsible=icon]`.
	// The rail would then show a gap where a section is, with no way to recover it.
	const sidebar = useSidebar();

	/** The groups the reader collapsed or opened by hand, by label: their choice holds until they
	 *  change it, including while they navigate. */
	let chosen = $state<Record<string, boolean>>({});

	/** A group is open as the reader left it; until they choose, when it is not declared collapsed or
	 *  holds the active route. Always open while the rail is collapsed to icons, where "closed" would
	 *  mean the section's icons are simply absent. */
	function groupOpen(group: PageNavGroup, p: string): boolean {
		if (sidebar.state === 'collapsed') return true;
		const choice = group.label === undefined ? undefined : chosen[group.label];
		return choice ?? (!group.defaultCollapsed || holdsActive(group.items, p));
	}

	function holdsActive(items: PageNavLeaf[], p: string): boolean {
		return items.some((i) => isActive(i, p));
	}

	/** Each distinct reason a group's rows cannot be opened, said once under the group. */
	function refusals(items: PageNavLeaf[]): string[] {
		return [...new Set(items.flatMap((i) => (i.unavailable ? [i.unavailable] : [])))];
	}

	const uid = $props.id();
	const noteId = (group: number, reason: number) => `${uid}-refusal-${group}-${reason}`;

	/** The mobile sheet closes once the reader picks a page in it. */
	function picked(): void {
		if (sidebar.isMobile) sidebar.setOpenMobile(false);
	}

	/** Active if the leaf matches, or any of its children do (so a parent highlights and expands
	 *  while you are inside it). */
	function isActive(leaf: PageNavLeaf, p: string): boolean {
		return leaf.match(p) || (leaf.children?.some((c) => c.match(p)) ?? false);
	}
</script>

{#snippet leafLink(
	leaf: PageNavLeaf,
	props: Record<string, unknown>,
	describedBy: string | undefined,
)}
	{#if leaf.unavailable}
		<!-- SHOWN, not hidden — and not a link. A <span> rather than a disabled <a>: an anchor with an
		     href is reachable by keyboard and by paste however it is styled, and this must not navigate
		     to a page that cannot render. `aria-disabled`, and the reason as its description: the
		     group's note below the rows, which a pointer, a keyboard and a touch all reach. -->
		<span
			{...props}
			aria-disabled="true"
			aria-describedby={describedBy}
			class="cursor-not-allowed opacity-50 {(props['class'] as string) ?? ''}"
		>
			{#if leaf.icon}
				<leaf.icon />
			{/if}
			<span>{leaf.title}</span>
		</span>
	{:else}
		<a href={leaf.href} {...mergeProps(props, { onclick: picked })}>
			{#if leaf.icon}
				<leaf.icon />
			{/if}
			<span>{leaf.title}</span>
		</a>
	{/if}
{/snippet}

{#snippet rows(items: PageNavLeaf[], groupIndex: number)}
	{@const notes = refusals(items)}
	<Sidebar.Menu>
		{#each items as leaf (leaf.title)}
			{@const describedBy = leaf.unavailable
				? noteId(groupIndex, notes.indexOf(leaf.unavailable))
				: undefined}
			<Sidebar.MenuItem>
				<Sidebar.MenuButton
					tooltipContent={leaf.unavailable ? `${leaf.title}: ${leaf.unavailable}` : leaf.title}
					isActive={isActive(leaf, pathname)}
				>
					{#snippet child({ props })}
						{@render leafLink(leaf, props, describedBy)}
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
												{@render leafLink(sub, props, undefined)}
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
	{#each notes as note, index (note)}
		<!-- The icon rail has no room for it: there the row's tooltip carries the same reason. -->
		<p
			id={noteId(groupIndex, index)}
			data-slot="sidebar-refusal"
			class="text-sidebar-foreground/70 px-2 pt-1 text-xs group-data-[collapsible=icon]:hidden"
		>
			{note}
		</p>
	{/each}
{/snippet}

{#if nav}
	{#each nav.groups as group, index (group.label ?? `rows-${index}`)}
		{#if group.label === undefined}
			<!-- Rows with no heading: the top of the rail, above every labelled section. -->
			<Sidebar.Group>
				<Sidebar.GroupContent>
					{@render rows(group.items, index)}
				</Sidebar.GroupContent>
			</Sidebar.Group>
		{:else}
			{@const label = group.label}
			{@const open = groupOpen(group, pathname)}
			<!-- A closed group that holds the page you are on says so on its heading. -->
			{@const marked = !open && holdsActive(group.items, pathname)}
			<Collapsible.Root
				{open}
				onOpenChange={(next) => (chosen[label] = next)}
				class="group/collapsible"
			>
				<Sidebar.Group>
					<Sidebar.GroupLabel>
						{#snippet child({ props })}
							<!-- mergeProps joins the label's classes with the mark's. -->
							<Collapsible.Trigger
								{...mergeProps(props, {
									'data-active': marked,
									class:
										'data-[active=true]:text-sidebar-accent-foreground data-[active=true]:font-semibold',
								})}
							>
								{label}
								{#if marked}
									<span class="bg-sidebar-primary ms-1.5 size-1.5 rounded-full" aria-hidden="true"
									></span>
									<span class="sr-only">, holds the current page</span>
								{/if}
								<ChevronRight
									class="ms-auto transition-transform group-data-[state=open]/collapsible:rotate-90"
								/>
							</Collapsible.Trigger>
						{/snippet}
					</Sidebar.GroupLabel>
					<Collapsible.Content>
						<Sidebar.GroupContent>
							{@render rows(group.items, index)}
						</Sidebar.GroupContent>
					</Collapsible.Content>
				</Sidebar.Group>
			</Collapsible.Root>
		{/if}
	{/each}
{/if}
