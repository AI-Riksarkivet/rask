<script lang="ts">
	import { page } from '$app/state';
	import { goto } from '$app/navigation';
	import * as Sidebar from '$lib/components/ui/sidebar';
	import { navGroups } from './nav-config';

	const path = $derived(page.url?.pathname ?? '');

	/** Viewer shortcut: open a random completed batch, falling back to /batches. */
	async function openRandomViewer() {
		try {
			const r = await fetch('/api/batches/random?status=done');
			if (!r.ok) return void goto('/batches');
			const { batch_id } = await r.json();
			await goto(`/viewer/${encodeURIComponent(batch_id)}`);
		} catch {
			await goto('/batches');
		}
	}
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
								{#if item.actionId === 'viewer-random'}
									<Sidebar.MenuButton
										isActive={item.match(path)}
										tooltipContent={item.title}
										onclick={openRandomViewer}
									>
										<item.icon />
										<span>{item.title}</span>
									</Sidebar.MenuButton>
								{:else}
									<Sidebar.MenuButton isActive={item.match(path)} tooltipContent={item.title}>
										{#snippet child({ props })}
											<a href={item.href} {...props}>
												<item.icon />
												<span>{item.title}</span>
											</a>
										{/snippet}
									</Sidebar.MenuButton>
								{/if}
							</Sidebar.MenuItem>
						{/each}
					</Sidebar.Menu>
				</Sidebar.GroupContent>
			</Sidebar.Group>
		{/each}
	</Sidebar.Content>

	<Sidebar.Rail />
</Sidebar.Root>
