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
		isMainMenu,
		mainMenuNav,
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
	// per microfrontend zone, and EVERY zone of the seven-zone estate is in the bar (R15); a zone
	// that carries `items`/`groups` renders as a trigger opening a panel of its sub-areas (so the
	// estate is one hop away from anywhere), and a zone with a single surface stays a plain link.
	// The Governance/Operations columns appear only for an estate admin (`me.estate_admin`, the
	// frozen /v1/me contract — fail-closed: no `me`, no admin columns); Access is never a top-level
	// entry, it is one row of the Governance column. The identity/theme control (the old sidebar
	// footer nav-user) lives on the right side. Bare flex chrome on purpose: the mount decides the
	// framing (AppShell sits it on the header's navbar row).
	//
	// `me` is the RESOLVED identity (null = signed out / lookup failed); `meLoading` renders the BASE
	// entry titles as invisible text under skeleton pills instead — the same chrome classes and the
	// same chevron reservation as the resolved entries, so loading and resolved states have IDENTICAL
	// dimensions (no layout shift when /v1/me lands) and a zone whose `me` is server-resolved never flashes
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

	// TWO LEVELS, TWO BARS. The ESTATE level — `/`, `/projects`, `/settings` — is where you choose what
	// to work on: Home · Projects · Settings. Everything else is inside a project and gets the zone
	// bar. `isMainMenu` owns that test (nav-config.ts), and the boundary is deliberate: `/projects` is
	// the list and stays at the estate level, while `/projects/<id>` is a project you have OPENED, so
	// it gets the zone bar — opening a project is what puts you inside one.
	//
	// It replaced `zoneOf(pathname) === ''`, which could not tell those two apart: both are home-zone
	// paths, and one of them is the whole point of the distinction.
	const inMainMenu = $derived(isMainMenu(pathname));
	const entries = $derived(
		inMainMenu ? mainMenuNav(me?.estate_admin ?? false) : topNav(me?.estate_admin ?? false),
	);
	// The identity-free base set: what the skeleton reserves space for while /v1/me is in flight
	// (an admin's extra panel columns append on resolve — earned content, not reserved chrome).
	// Skeletons must reserve the bar you are ACTUALLY about to get, or the main menu flashes eight
	// placeholder chips and settles into two.
	const placeholders = $derived(inMainMenu ? mainMenuNav(false) : topNav(false));
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

	// THE ESTATE BAR IS CENTRED; the zone bar is not. Inside a project this row is a menubar — it
	// starts where the sidebar trigger ends and reads left-to-right like every other strip of chrome
	// above content. The main menu has no rail, no crumbs and two or three entries in a full-width
	// bar, so left-aligning them there parked the estate's ONLY navigation in a corner with a screen
	// of dead space beside it.
	//
	// Centring takes a flexible spacer on BOTH flanks — the account cluster is the right one — because
	// the entries must sit in the middle of the BAR, not in the middle of whatever the account cluster
	// leaves over. `mx-auto` cannot do it: an auto margin is only paid out of free space, and the
	// flex-1 flank has already taken all of it.
	//
	// Not while collapsed: there the bar is one Menu trigger whose Root deliberately spans the row
	// (see below), and a centred single trigger would just look misplaced.
	const centred = $derived(inMainMenu && !collapsed);

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
				// A paneled zone's prepended root row says "<Zone> home" to set it off from the rows
				// under it; a plain-link zone's ONLY row IS the zone, so it just carries the zone's
				// name (the Home zone would otherwise read "Home home").
				title: items.length ? `${entry.title} home` : entry.title,
				href: entry.href,
				description: `Open the ${entry.title.toLowerCase()} zone.`,
			},
			...items,
		];
	};
</script>

<div class={cn('flex min-w-0 items-center gap-2', className)}>
	{#if centred}
		<!-- The LEFT flank. Empty and non-semantic on purpose: its only job is to be exactly as wide as
		     the account cluster's flank, which is what puts the entries in the middle of the bar. -->
		<div class="flex-1" data-slot="navbar-flank"></div>
	{/if}
	<!-- `aria-label` overrides the primitive's default "main", so this IS the zones landmark — one
	     nav element, not a nav nested inside a nav. Collapsed, the root takes the whole row (the
	     base `max-w-max` is dropped) so the shared panel viewport — which is `w-full` below `md` —
	     is bounded by the row instead of by one narrow trigger, and cannot spill past the edge.
	     Centred, it is `flex-none` instead: the base `flex-1` would have it share the row's free space
	     with the two flanks, and an entry group that grows with the window is not a centred one. -->
	<NavigationMenu.Root
		aria-label="Zones"
		class={cn('min-w-0', collapsed && 'max-w-none flex-1', centred && 'flex-none')}
	>
		<NavigationMenu.List class="justify-start gap-0.5">
			{#if meLoading}
				{#each collapsed ? placeholders.slice(0, 1) : placeholders as entry, i (entry.title)}
					<!-- The tier gap is RESERVED here too, on the same rule as the resolved bar — and under
					     the same `inMainMenu` guard, or the two states disagree about a 56px hole. It is
					     chrome, not content: leaving it out made the row 26px narrower while /v1/me was in
					     flight and 26px wider the instant it landed — a layout shift measured by
					     `the loading skeleton reserves the resolved entry widths`, which is exactly the
					     regression that test exists to catch. Skeleton and resolved must reserve the SAME
					     boxes, gaps included. -->
					{#if !inMainMenu && !collapsed && i > 0 && entry.tier !== 'primary' && placeholders[i - 1]?.tier === 'primary'}
						<li role="none" class="w-4 shrink-0 sm:w-6"></li>
					{/if}
					<li aria-hidden="true">
						<span class={cn(chrome, 'relative')}>
							{#if collapsed}
								<Menu class="invisible size-4" />
							{:else if entry.icon}
								<entry.icon class="invisible size-4" />
							{/if}
							<span class="invisible">{collapsed ? 'Menu' : entry.title}</span>
							{#if collapsed || entry.items || entry.groups}
								<!-- Reserve the trigger's chevron too, or an entry with a panel would grow by
								     its width the moment the identity lands. Collapsed, the one real entry is
								     ALWAYS the Menu trigger, so the chevron is reserved regardless of what the
								     first placeholder entry happens to be. -->
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
									class="text-muted-foreground flex items-center gap-1.5 px-2 pt-1 pb-1.5 text-[0.6875rem] font-semibold tracking-wide uppercase"
								>
									{#if entry.icon}
										<entry.icon class="size-3.5" aria-hidden="true" />
									{/if}
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
				{#each entries as entry, i (entry.title)}
					<!-- THE TIER GAP — A ZONE-BAR DEVICE, and only that. `tier: 'primary'` already told the
					     bar which entries lead — the lakehouse you govern and the compute that fills it —
					     but nothing rendered that weighting, so eight equal chips told a newcomer nothing
					     about where to start. One spacer at the boundary, drawn from the data rather than
					     from a hardcoded index, so re-tiering an entry moves the gap with it.
					     `role="none"` ALONE, not `aria-hidden` beside it: the two are not combinable
					     (aria-hidden is unsupported on role=none, and the svelte a11y checker rejects the
					     pair), and `none` is the right one. It strips the listitem semantics, so a screen
					     reader walking the list never meets an empty item between two real ones — exactly
					     what a decorative spacer should do.
					     NOT IN THE MAIN MENU. Weighting eight zone entries is guidance; weighting three
					     estate entries is a hole. It was drawn there at DOUBLE width (sm:w-14) to read as
					     structure and read instead as Settings having fallen off the end of the row — and
					     the skeleton above reserved the compact gap, so an admin's bar also jumped 32px
					     when /v1/me landed. Home, Projects and Settings are the three places you can be at
					     this level, peers by construction: one rhythm, no spacer. -->
					{#if !inMainMenu && i > 0 && entry.tier !== 'primary' && entries[i - 1]?.tier === 'primary'}
						<li role="none" class="w-4 shrink-0 sm:w-6" data-slot="navbar-tier-gap"></li>
					{/if}
					<NavigationMenu.Item>
						{#if entry.groups}
							<!-- A trigger spanning SEVERAL concerns (Lakehouse: the catalog, the model
							     registry and — for an estate admin — governance/operations over the same
							     estate) renders labelled columns, so the panel explains the shape instead
							     of listing a dozen undifferentiated rows. Column count follows the groups
							     the viewer actually gets, so a non-admin sees a tighter two-column panel. -->
							<NavigationMenu.Trigger data-active={entry.match(pathname) ? '' : undefined}>
								{#if entry.icon}
									<entry.icon class="size-4" aria-hidden="true" />
								{/if}
								{entry.title}
							</NavigationMenu.Trigger>
							<NavigationMenu.Content>
								<div
									class="grid gap-x-3 gap-y-1 p-2 md:w-[46rem]"
									style="grid-template-columns: repeat({entry.groups.length}, minmax(0, 1fr))"
								>
									<!-- No zone-root filler row here, BY RULING (2026-08-03): the groups panel's
									     destinations are its group rows — a "Lakehouse · Open the lakehouse zone." row
									     linking a redirect-to-tables page said nothing a real row doesn't, and only
									     THIS zone ever rendered it (the flat-items zones all carry a real
									     function-titled root row — Search, Overview — so their equivalent is skipped).
									     The narrow overflow menu still prepends every zone's root uniformly; the wide
									     panel does not. -->
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
								{#if entry.icon}
									<entry.icon class="size-4" aria-hidden="true" />
								{/if}
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
								{#if entry.icon}
									<entry.icon class="size-4" aria-hidden="true" />
								{/if}
								{entry.title}
							</NavigationMenu.Link>
						{/if}
					</NavigationMenu.Item>
				{/each}
			{/if}
		</NavigationMenu.List>
	</NavigationMenu.Root>
	<!-- The RIGHT flank. `ml-auto` pins it to the end of a left-aligned bar; centred, it is the
	     growing counterweight to the empty flank above, and the entries land between two equal
	     halves. Either way its own contents stay hard right and at their natural size. -->
	<div class={cn('flex items-center', centred ? 'flex-1 justify-end' : 'ml-auto shrink-0')}>
		{#if meLoading}
			<!-- size-8 = the resolved NavbarUser trigger (Button size="icon") — same box, no shift. The
			     bell reserves nothing: a zone with no feed has no bell, so reserving its width would
			     shift the account control on every load in every zone that has not wired a feed. -->
			<Skeleton class="size-8 rounded-full" />
		{:else}
			{#if notifications}
				<!-- Run lifecycle, beside the account cluster and in the same box size — the one place in
				     the estate that says a run started, finished, or failed. -->
				<NotificationCenter
					runs={notifications.runs}
					inbox={notifications.inbox}
					inboxUnread={notifications.inboxUnread}
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
