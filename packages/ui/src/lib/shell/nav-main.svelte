<script lang="ts">
	import * as Collapsible from '../components/collapsible/index.js';
	import * as Sidebar from '../components/sidebar/index.js';
	import { ChevronRight } from '@lucide/svelte';
	import { navMain } from './nav-config.js';

	// sidebar-07 NavMain, adapted: each domain is a collapsible accordion whose
	// PARENT is a link to its landing route (so clicking "Compute" goes to overview)
	// and whose chevron (a MenuAction) toggles the sub-routes. Collapsible.Root's
	// `child` snippet makes the MenuItem the group element (group/collapsible +
	// data-state), so the chevron's group-data rotation works. Single-route domains
	// render as a plain link. `pathname` drives active + default-open.
	let { pathname = '' }: { pathname?: string } = $props();

	// Project-first IA: the active project is the URL's first segment, so the nav's
	// hrefs are prefixed with it (the sidebar only ever renders inside a project).
	const project = $derived(pathname.split('/').filter(Boolean)[0] ?? 'default');
	const items = $derived(navMain(project));

	// MFE zones split by DOMAIN (the segment after the project): /<project>/compute is a
	// different SvelteKit app/zone than /<project>/discover. A sidebar link whose domain
	// differs from the current one leaves THIS app's route manifest, so it must hard-nav
	// (data-sveltekit-reload) — a soft client nav would target a route this app doesn't know.
	// Same-domain links (a domain's own sub-routes) stay soft for SPA speed.
	const currentDomain = $derived(pathname.split('/').filter(Boolean)[1] ?? '');
	const crossZone = (href: string) => (href.split('/').filter(Boolean)[1] ?? '') !== currentDomain;
</script>

<Sidebar.Group>
	<Sidebar.Menu>
		{#each items as item (item.title)}
			{#if item.items?.length}
				<Collapsible.Root open={item.match(pathname)} class="group/collapsible">
					{#snippet child({ props: rootProps })}
						<Sidebar.MenuItem {...rootProps}>
							<!-- Accordion parent links to its landing; highlights ONLY on that exact
							     landing (e.g. /compute = overview), never on a sub-route — so no
							     double-highlight with the active leaf. -->
							<Sidebar.MenuButton tooltipContent={item.title} isActive={pathname === item.href}>
								{#snippet child({ props })}
									<a
										href={item.href}
										data-sveltekit-reload={crossZone(item.href) ? '' : undefined}
										{...props}
									>
										<item.icon />
										<span>{item.title}</span>
									</a>
								{/snippet}
							</Sidebar.MenuButton>
							<Collapsible.Trigger>
								{#snippet child({ props })}
									<Sidebar.MenuAction
										{...props}
										class="transition-transform duration-200 group-data-[state=open]/collapsible:rotate-90"
									>
										<ChevronRight />
										<span class="sr-only">Toggle {item.title}</span>
									</Sidebar.MenuAction>
								{/snippet}
							</Collapsible.Trigger>
							<Collapsible.Content>
								<Sidebar.MenuSub>
									{#each item.items ?? [] as sub (sub.title)}
										<Sidebar.MenuSubItem>
											<Sidebar.MenuSubButton isActive={sub.match(pathname)}>
												{#snippet child({ props })}
													<a
														href={sub.href}
														data-sveltekit-reload={crossZone(sub.href) ? '' : undefined}
														{...props}
													>
														<span>{sub.title}</span>
													</a>
												{/snippet}
											</Sidebar.MenuSubButton>
										</Sidebar.MenuSubItem>
									{/each}
								</Sidebar.MenuSub>
							</Collapsible.Content>
						</Sidebar.MenuItem>
					{/snippet}
				</Collapsible.Root>
			{:else}
				<Sidebar.MenuItem>
					<Sidebar.MenuButton tooltipContent={item.title} isActive={item.match(pathname)}>
						{#snippet child({ props })}
							<a
								href={item.href}
								data-sveltekit-reload={crossZone(item.href) ? '' : undefined}
								{...props}
							>
								<item.icon />
								<span>{item.title}</span>
							</a>
						{/snippet}
					</Sidebar.MenuButton>
				</Sidebar.MenuItem>
			{/if}
		{/each}
	</Sidebar.Menu>
</Sidebar.Group>
