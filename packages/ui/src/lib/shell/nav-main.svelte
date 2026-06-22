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
	// render as a plain link. `pathname` (incl. base) drives active + default-open.
	let { pathname = '' }: { pathname?: string } = $props();
</script>

<Sidebar.Group>
	<Sidebar.Menu>
		{#each navMain as item (item.title)}
			{#if item.items?.length}
				<Collapsible.Root open={item.match(pathname)} class="group/collapsible">
					{#snippet child({ props: rootProps })}
						<Sidebar.MenuItem {...rootProps}>
							<Sidebar.MenuButton tooltipContent={item.title} isActive={item.match(pathname)}>
								{#snippet child({ props })}
									<a href={item.href} {...props}>
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
													<a href={sub.href} {...props}>
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
							<a href={item.href} {...props}>
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
