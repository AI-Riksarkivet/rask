<script lang="ts">
	import { Badge } from '@rask/ui/badge';
	import { Button } from '@rask/ui/button';
	import { Card } from '@rask/ui/card';
	import type { PageData } from './$types';

	let { data }: { data: PageData } = $props();
</script>

<svelte:head><title>lance</title></svelte:head>

<div class="px-4 py-10">
	<div class="mx-auto flex w-full max-w-5xl flex-col gap-6">
		<header class="flex flex-col gap-1">
			<h1 class="text-3xl font-semibold">lance</h1>
			<p class="text-muted-foreground">
				Governed Lance lakehouse — {data.estateAdmin ? 'every project in the estate' : 'your projects'}.
			</p>
		</header>

		{#if data.projects.length > 0}
			<div class="grid gap-4 sm:grid-cols-2 lg:grid-cols-3">
				{#each data.projects as p (p.project)}
					<!-- Cross-zone card into the data zone's project page (hard nav). -->
					<a
						href={`/lakehouse/data/projects/${p.project}`}
						data-sveltekit-reload
						class="group block"
					>
						<Card
							class="hover:border-ring/40 hover:bg-accent flex h-full flex-col gap-2 p-5 transition-colors"
						>
							<div class="flex items-start justify-between gap-2">
								<div class="truncate text-lg font-medium">{p.project}</div>
								{#if p.role}
									<Badge variant={p.role === 'admin' ? 'default' : 'secondary'}>{p.role}</Badge>
								{/if}
							</div>
							<div class="text-muted-foreground text-sm">
								{#if p.warehouses !== null}
									{p.warehouses}
									{p.warehouses === 1 ? 'warehouse' : 'warehouses'}
								{:else}
									project
								{/if}
							</div>
						</Card>
					</a>
				{/each}
			</div>
		{:else}
			<!-- Empty state: signed out (prompt), or signed in with no memberships. -->
			<div
				class="border-border flex flex-col items-center gap-3 rounded-lg border border-dashed p-10 text-center"
			>
				{#if data.signedIn}
					<p class="text-muted-foreground">
						You are not a member of any project yet. Ask a project admin for access.
					</p>
				{:else if data.authEnabled}
					<p class="text-muted-foreground">Sign in to see your projects.</p>
					<Button href="/auth/login?redirect=/">Sign in</Button>
				{:else}
					<p class="text-muted-foreground">
						No projects to show — sign-in is not configured on this stack.
					</p>
				{/if}
			</div>
		{/if}
	</div>
</div>
