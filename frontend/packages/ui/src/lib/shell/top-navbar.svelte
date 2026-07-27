<script lang="ts">
	import * as NavigationMenu from '../components/navigation-menu/index.js';
	import { navigationMenuTriggerStyle } from '../components/navigation-menu/index.js';
	import { Skeleton } from '../components/skeleton/index.js';
	import NavbarUser from './navbar-user.svelte';
	import NotificationCenter from './notification-center.svelte';
	import { cn } from '../utils/cn.js';
	import { IsMobile, SHELL_COLLAPSE_BREAKPOINT } from '../hooks/is-mobile.svelte.js';
	import { ChevronDown, Menu } from '@lucide/svelte';
	import {
		norm,
		prefetchOnIntent,
		topNav,
		under,
		zoneOf,
		type Me,
		type NavUser,
		type NotificationFeed,
		type TopNavEntry,
		type TopNavItem,
	} from './nav-config.js';

	// The shared top navbar — the cross-zone IA on the shadcn-svelte NavigationMenu shape. One entry
	// per microfrontend zone; a zone that carries `items` renders as a trigger opening a panel of its
	// sub-areas (so the estate is one hop away from anywhere), and a zone with a single surface stays
	// a plain link. Admin appears only for an estate admin (`me.estate_admin`, the frozen /v1/me
	// contract — fail-closed: no `me`, no admin entry); Access is never a top-level entry, it is one
	// row of Admin's panel. The identity/theme control (the old sidebar footer nav-user) lives on the
	// right side. Bare flex chrome on purpose: the mount decides the framing (AppShell sits it on the
	// header's navbar row).
	//
	// `me` is the RESOLVED identity (null = signed out / lookup failed); `meLoading` renders the BASE
	// entry titles as invisible text under skeleton pills instead — the same chrome classes and the
	// same chevron reservation as the resolved entries, so loading and resolved states have IDENTICAL
	// dimensions (no layout shift when /v1/me lands) and a zone streaming `fetchMe()` never flashes
	// the non-admin entry set pretending to be the truth.
	let {
		pathname = '',
		me = null,
		meLoading = false,
		authEnabled = false,
		user = null,
		notifications = null,
		class: className,
	}: {
		pathname?: string;
		/** The frozen /v1/me identity, or null when signed out / unresolved. */
		me?: Me | null;
		/** True while the zone's fetchMe() is still in flight — renders skeletons. */
		meLoading?: boolean;
		authEnabled?: boolean;
		user?: NavUser | null;
		/** The zone's run feed for the notification bell. `null` (the default) renders NO bell — a zone
		 *  opts in by passing its runs, so a zone that has not wired the feed shows nothing rather than
		 *  a permanently empty surface. */
		notifications?: NotificationFeed | null;
		class?: string;
	} = $props();

	const entries = $derived(topNav(me?.estate_admin ?? false));
	// The identity-free base set: what the skeleton reserves space for while /v1/me is in flight
	// (an admin's extra entry appends on resolve — earned content, not reserved chrome).
	const placeholders = topNav(false);
	// Cross-zone links leave THIS app's route manifest → hard nav (data-sveltekit-reload); the home
	// zone owns the origin root, so its zone key is ''. A plain function over the `pathname` prop, not
	// a `$derived`: the panel rows render inside bits-ui's portalled Content, and a derived read from
	// there warns (`derived_inert`) as the panel tears down.
	const crossZone = (href: string) => zoneOf(href) !== zoneOf(pathname);

	// Warm a cross-zone target document on intent (hover/focus): rel=prefetch of the zone root —
	// see prefetchDocument for the honest browser-support scope. Same-zone links already preload
	// via data-sveltekit-preload-data. Attachment (native listeners), so bits-ui's own pointer
	// handlers on the link are never clobbered.
	const warm = (href: string) => (el: HTMLElement) =>
		crossZone(href) ? prefetchOnIntent(href)(el) : undefined;

	// A panel row is lit when the current path is under it — except a row that IS the zone root
	// (Lineage's Graph, Media's Search), which matches exactly or it would light up on every sibling.
	const itemActive = (entry: TopNavEntry, item: TopNavItem) =>
		item.href === entry.href ? norm(pathname) === item.href : under(item.href)(pathname);

	// One chrome class list for the resolved entries AND the loading placeholders, so the two states
	// cannot drift apart dimensionally — and shared with the plain links, so a link and a trigger are
	// the same box.
	const chrome = navigationMenuTriggerStyle();

	// Below the shell breakpoint the whole bar folds into ONE overflow entry. The zone entries are
	// `whitespace-nowrap` by design (a wrapped nav label is worse than no nav label), so they cannot
	// share a narrow row with the project switcher and the account control — they used to simply
	// overflow their container and run underneath the avatar. Collapsing is the only honest answer:
	// one trigger, and every destination still one tap away inside its panel. Same constant the
	// sidebar folds on, so the shell never disagrees with itself.
	const narrow = new IsMobile(SHELL_COLLAPSE_BREAKPOINT);
	const collapsed = $derived(narrow.current);

	// The overflow panel is FLAT — one heading per zone, then its destinations. The desktop panels'
	// group columns (Lakehouse's Catalog/Models/Governance/Operations) and the row descriptions are
	// dropped: on a phone-width panel they cost a screenful of scrolling and buy nothing. The zone
	// root is prepended when no row already is it, exactly like the desktop panel, so a trigger is
	// never the only way into a zone.
	const overflowItems = (entry: TopNavEntry): TopNavItem[] => {
		const items = entry.groups ? entry.groups.flatMap((group) => group.items) : (entry.items ?? []);
		if (items.some((item) => item.href === entry.href)) return items;
		return [
			{
				title: `${entry.title} home`,
				href: entry.href,
				description: `Open the ${entry.title.toLowerCase()} zone.`,
			},
			...items,
		];
	};
</script>

<div class={cn('flex min-w-0 items-center gap-2', className)}>
	<!-- `aria-label` overrides the primitive's default "main", so this IS the zones landmark — one
	     nav element, not a nav nested inside a nav. Collapsed, the root takes the whole row (the
	     base `max-w-max` is dropped) so the shared panel viewport — which is `w-full` below `md` —
	     is bounded by the row instead of by one narrow trigger, and cannot spill past the edge. -->
	<NavigationMenu.Root aria-label="Zones" class={cn('min-w-0', collapsed && 'max-w-none flex-1')}>
		<NavigationMenu.List class="justify-start gap-0.5">
			{#if meLoading}
				{#each collapsed ? placeholders.slice(0, 1) : placeholders as entry (entry.title)}
					<li aria-hidden="true">
						<span class={cn(chrome, 'relative')}>
							<span class="invisible">{collapsed ? 'Menu' : entry.title}</span>
							{#if entry.items || entry.groups}
								<!-- Reserve the trigger's chevron too, or an entry with a panel would grow by
								     its width the moment the identity lands. -->
								<ChevronDown class="invisible size-3" />
							{/if}
							<Skeleton class="absolute inset-0 rounded-lg" />
						</span>
					</li>
				{/each}
			{:else if collapsed}
				<!-- The narrow bar: ONE entry. Every zone and every sub-area it owns is a row in this
				     panel, so nothing the wide bar reaches becomes unreachable here. -->
				<NavigationMenu.Item>
					<NavigationMenu.Trigger>
						<Menu class="size-4" aria-hidden="true" />
						Menu
					</NavigationMenu.Trigger>
					<NavigationMenu.Content>
						<div class="max-h-[70svh] overflow-y-auto p-2">
							{#each entries as entry (entry.title)}
								<p
									data-slot="navbar-overflow-group"
									class="text-muted-foreground px-2 pt-1 pb-1.5 text-[0.6875rem] font-semibold tracking-wide uppercase"
								>
									{entry.title}
								</p>
								<ul class="grid gap-0.5 pb-1">
									{#each overflowItems(entry) as item (item.href)}
										<li>
											<NavigationMenu.Link
												href={item.href}
												active={itemActive(entry, item)}
												data-sveltekit-reload={crossZone(item.href) ? '' : undefined}
												{@attach warm(item.href)}
											>
												<span class="truncate text-sm leading-none font-medium">{item.title}</span>
											</NavigationMenu.Link>
										</li>
									{/each}
								</ul>
							{/each}
						</div>
					</NavigationMenu.Content>
				</NavigationMenu.Item>
			{:else}
				{#each entries as entry (entry.title)}
					<NavigationMenu.Item>
						{#if entry.groups}
							<!-- A trigger spanning SEVERAL concerns (Lakehouse: the catalog, the model
							     registry and — for an estate admin — governance/operations over the same
							     estate) renders labelled columns, so the panel explains the shape instead
							     of listing a dozen undifferentiated rows. Column count follows the groups
							     the viewer actually gets, so a non-admin sees a tighter two-column panel. -->
							<NavigationMenu.Trigger data-active={entry.match(pathname) ? '' : undefined}>
								{entry.title}
							</NavigationMenu.Trigger>
							<NavigationMenu.Content>
								<div
									class="grid gap-x-3 gap-y-1 p-2 md:w-[46rem]"
									style="grid-template-columns: repeat({entry.groups.length}, minmax(0, 1fr))"
								>
									{#if !entry.groups.some((g) => g.items.some((i) => i.href === entry.href))}
										<!-- The zone root, on the SAME terms as the `items` branch below: a panel must not
										     be the only way in, and the trigger is a `<button>`, not a link. This branch
										     shipped without it, which left the Lakehouse zone root reachable on a wide
										     screen only by breadcrumb or by typing the URL — while the narrow bar's
										     `overflowItems` comment claimed it prepends the root "exactly like the desktop
										     panel". Skipped when a group row already IS the root, so no href appears twice.

										     INSIDE the grid, spanning every column — NOT a sibling before it. As a sibling
										     it gave Content two children, and bits-ui sizes the shared viewport from the
										     active content: the viewport measured 69px (this row's height) and CLIPPED all
										     five columns. Every link was still in the DOM and Playwright still called them
										     visible, so an href-presence assertion passed while the panel showed one row.
										     The screenshot is what caught it. -->
										<div style="grid-column: 1 / -1">
											<NavigationMenu.Link
												href={entry.href}
												active={norm(pathname) === entry.href}
												data-sveltekit-reload={crossZone(entry.href) ? '' : undefined}
												{@attach warm(entry.href)}
												class="bg-muted/40 p-3"
											>
												<span class="text-foreground text-sm leading-none font-medium">{entry.title}</span>
												<span class="text-muted-foreground text-xs leading-snug">
													Open the {entry.title.toLowerCase()} zone.
												</span>
											</NavigationMenu.Link>
										</div>
									{/if}
									{#each entry.groups as group (group.label)}
										<div>
											<p
												class="text-muted-foreground px-3 pt-1 pb-1.5 text-[0.6875rem] font-semibold tracking-wide uppercase"
											>
												{group.label}
											</p>
											<ul class="grid gap-1">
												{#each group.items as item (item.href)}
													<li>
														<NavigationMenu.Link
															href={item.href}
															active={itemActive(entry, item)}
															data-sveltekit-reload={crossZone(item.href) ? '' : undefined}
															{@attach warm(item.href)}
														>
															<span class="text-sm leading-none font-medium">{item.title}</span>
															<span class="text-muted-foreground line-clamp-2 text-xs leading-snug">
																{item.description}
															</span>
														</NavigationMenu.Link>
													</li>
												{/each}
											</ul>
										</div>
									{/each}
								</div>
							</NavigationMenu.Content>
						{:else if entry.items}
							<NavigationMenu.Trigger data-active={entry.match(pathname) ? '' : undefined}>
								{entry.title}
							</NavigationMenu.Trigger>
							<NavigationMenu.Content>
								<ul class="grid gap-1 p-2 md:w-[30rem] md:grid-cols-2">
									{#if !entry.items.some((i) => i.href === entry.href)}
										<!-- The zone root itself — a panel must not be the only way in, and the
										     trigger is a button, not a link. Skipped when a row already IS the
										     root (lineage's Graph, media's Search), so no href appears twice. -->
										<li class="md:col-span-2">
											<NavigationMenu.Link
												href={entry.href}
												active={norm(pathname) === entry.href}
												data-sveltekit-reload={crossZone(entry.href) ? '' : undefined}
												{@attach warm(entry.href)}
												class="bg-muted/40 p-3"
											>
												<span class="text-foreground text-sm leading-none font-medium">{entry.title}</span>
												<span class="text-muted-foreground text-xs leading-snug">
													Open the {entry.title.toLowerCase()} zone.
												</span>
											</NavigationMenu.Link>
										</li>
									{/if}
									{#each entry.items as item (item.href)}
										<li>
											<NavigationMenu.Link
												href={item.href}
												active={itemActive(entry, item)}
												data-sveltekit-reload={crossZone(item.href) ? '' : undefined}
												{@attach warm(item.href)}
											>
												<span class="text-sm leading-none font-medium">{item.title}</span>
												<span class="text-muted-foreground line-clamp-2 text-xs leading-snug">
													{item.description}
												</span>
											</NavigationMenu.Link>
										</li>
									{/each}
								</ul>
							</NavigationMenu.Content>
						{:else}
							<NavigationMenu.Link
								href={entry.href}
								active={entry.match(pathname)}
								data-sveltekit-reload={crossZone(entry.href) ? '' : undefined}
								{@attach warm(entry.href)}
								class={chrome}
							>
								{entry.title}
							</NavigationMenu.Link>
						{/if}
					</NavigationMenu.Item>
				{/each}
			{/if}
		</NavigationMenu.List>
	</NavigationMenu.Root>
	<div class="ml-auto flex shrink-0 items-center">
		{#if meLoading}
			<!-- size-8 = the resolved NavbarUser trigger (Button size="icon") — same box, no shift. The
			     bell reserves nothing: a zone with no feed has no bell, so reserving its width would
			     shift the account control on every load in three of the four zones. -->
			<Skeleton class="size-8 rounded-full" />
		{:else}
			{#if notifications}
				<!-- Run lifecycle, beside the account cluster and in the same box size — the one place in
				     the estate that says a run started, finished, or failed. -->
				<NotificationCenter
					runs={notifications.runs}
					seen={notifications.seen}
					dismissed={notifications.dismissed}
					onseen={notifications.onseen}
					ondismiss={notifications.ondismiss}
					allHref={notifications.allHref}
				/>
			{/if}
			<NavbarUser {user} {authEnabled} {pathname} />
		{/if}
	</div>
</div>
