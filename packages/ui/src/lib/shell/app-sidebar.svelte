<script lang="ts">
	import * as Sidebar from '../components/sidebar/index.js';
	import { navGroups } from './nav-config.js';

	// Pure shared component: the consuming app passes the current pathname (from
	// $app/state) — the lib can't import $app/*. All items are plain <a href> so
	// cross-microfrontend navigation is a correct full-page load.
	let { pathname = '' }: { pathname?: string } = $props();
</script>

<Sidebar.Root collapsible="icon">
	<Sidebar.Header>
		<Sidebar.Menu>
			<Sidebar.MenuItem>
				<Sidebar.MenuButton size="lg">
					{#snippet child({ props })}
						<a href="/overview" {...props}>
							<div
								class="bg-sidebar-primary text-sidebar-primary-foreground flex aspect-square size-8 items-center justify-center rounded-lg"
							>
								<svg
									viewBox="0 0 24 24"
									class="size-5"
									fill="none"
									stroke="currentColor"
									stroke-width="2.2"
									stroke-linecap="round"
									stroke-linejoin="round"
								>
									<path d="M5 6l7 6-7 6" />
									<path d="M13 6l7 6-7 6" />
								</svg>
							</div>
							<div class="grid flex-1 text-left text-sm leading-tight">
								<span class="truncate font-semibold tracking-tight">RASK</span>
								<span class="text-muted-foreground truncate text-xs">HTR pipeline</span>
							</div>
						</a>
					{/snippet}
				</Sidebar.MenuButton>
			</Sidebar.MenuItem>
		</Sidebar.Menu>
	</Sidebar.Header>

	<Sidebar.Content>
		{#each navGroups as group (group.label)}
			<Sidebar.Group>
				<Sidebar.GroupLabel>{group.label}</Sidebar.GroupLabel>
				<Sidebar.GroupContent>
					<Sidebar.Menu>
						{#each group.items as item (item.title)}
							<Sidebar.MenuItem>
								<Sidebar.MenuButton isActive={item.match(pathname)} tooltipContent={item.title}>
									{#snippet child({ props })}
										<a href={item.href} {...props}>
											<item.icon />
											<span>{item.title}</span>
										</a>
									{/snippet}
								</Sidebar.MenuButton>
							</Sidebar.MenuItem>
						{/each}
					</Sidebar.Menu>
				</Sidebar.GroupContent>
			</Sidebar.Group>
		{/each}
	</Sidebar.Content>

	<Sidebar.Rail />
</Sidebar.Root>
