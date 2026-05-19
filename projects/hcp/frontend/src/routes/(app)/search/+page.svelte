<script lang="ts">
	import { page } from '$app/state';
	import { FileSearch, Activity, CircleHelp } from 'lucide-svelte';
	import { Button } from '$lib/components/ui/button/index.js';
	import { cn } from '$lib/utils.js';
	import PageHeader from '$lib/components/custom/page-header/page-header.svelte';
	import NoTenantPlaceholder from '$lib/components/custom/no-tenant-placeholder/no-tenant-placeholder.svelte';
	import SearchObjects from './sections/search-objects.svelte';
	import SearchOperations from './sections/search-operations.svelte';
	import SearchHelp from './sections/search-help.svelte';

	let tenant = $derived(page.data.tenant as string | undefined);

	let mode = $state<'objects' | 'operations'>('objects');
	let helpOpen = $state(false);
</script>

<svelte:head>
	<title>Search - HCP Admin Console</title>
</svelte:head>

<div class="space-y-4">
	<PageHeader
		title="Search"
		description="Query object metadata and audit operations across namespaces"
	/>

	{#if tenant}
		<!-- Mode switch -->
		<div class="flex items-center gap-4">
			<div class="flex gap-1 rounded-lg border bg-muted/50 p-1">
				<button
					class={cn(
						'flex items-center gap-1.5 rounded-md px-3 py-1.5 text-sm font-medium transition-colors',
						mode === 'objects'
							? 'bg-background text-foreground shadow-sm'
							: 'text-muted-foreground hover:text-foreground'
					)}
					onclick={() => (mode = 'objects')}
				>
					<FileSearch class="h-3.5 w-3.5" />
					Objects
				</button>
				<button
					class={cn(
						'flex items-center gap-1.5 rounded-md px-3 py-1.5 text-sm font-medium transition-colors',
						mode === 'operations'
							? 'bg-background text-foreground shadow-sm'
							: 'text-muted-foreground hover:text-foreground'
					)}
					onclick={() => (mode = 'operations')}
				>
					<Activity class="h-3.5 w-3.5" />
					Operations
				</button>
			</div>
			<Button
				variant="ghost"
				size="sm"
				class="gap-1 text-muted-foreground"
				onclick={() => (helpOpen = !helpOpen)}
			>
				<CircleHelp class="h-4 w-4" />
				<span class="hidden sm:inline">How does this work?</span>
			</Button>
		</div>

		<!-- Help panel -->
		{#if helpOpen}
			<SearchHelp {mode} />
		{/if}

		{#if mode === 'objects'}
			<SearchObjects {tenant} />
		{:else}
			<SearchOperations {tenant} />
		{/if}
	{:else}
		<NoTenantPlaceholder message="Log in with a tenant to search." />
	{/if}
</div>
