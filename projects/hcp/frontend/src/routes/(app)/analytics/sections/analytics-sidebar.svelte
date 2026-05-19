<script lang="ts">
	import * as Card from '$lib/components/ui/card/index.js';
	import { Input } from '$lib/components/ui/input/index.js';
	import { Label } from '$lib/components/ui/label/index.js';
	import { Button } from '$lib/components/ui/button/index.js';
	import { Badge } from '$lib/components/ui/badge/index.js';
	import { Skeleton } from '$lib/components/ui/skeleton/index.js';
	import ErrorBanner from '$lib/components/custom/error-banner/error-banner.svelte';
	import { get_lance_tables, get_lance_schema } from '$lib/remote/lance.remote.js';
	import type { LanceField } from '$lib/types/lance.js';
	import { Database, Table2, RefreshCw, FileType, Waypoints } from 'lucide-svelte';

	let {
		tenant,
		selectedBucket = $bindable(''),
		selectedPath = $bindable(''),
		selectedTable = $bindable(''),
	}: {
		tenant: string;
		selectedBucket: string;
		selectedPath: string;
		selectedTable: string;
	} = $props();

	let bucket = $state('');
	let path = $state('');

	let tablesData = $derived(
		selectedBucket
			? get_lance_tables({ bucket: selectedBucket, path: selectedPath || undefined })
			: undefined
	);
	let tables = $derived((tablesData?.current?.tables ?? []) as string[]);
	let error = $derived(tablesData?.current?.error ?? null);

	let schemaData = $derived(
		selectedTable
			? get_lance_schema({
					bucket: selectedBucket,
					path: selectedPath || undefined,
					table: selectedTable,
				})
			: undefined
	);
	let fields = $derived((schemaData?.current?.fields ?? []) as LanceField[]);

	function connect() {
		selectedBucket = bucket;
		selectedPath = path;
		selectedTable = '';
	}

	function selectTable(name: string) {
		selectedTable = name;
	}
</script>

<div class="space-y-4">
	<Card.Root>
		<Card.Header>
			<Card.Title class="flex items-center gap-2">
				<Database class="h-4 w-4" />
				S3 Location
			</Card.Title>
		</Card.Header>
		<Card.Content class="space-y-3">
			<div class="space-y-1">
				<Label for="lance-bucket">Bucket</Label>
				<Input id="lance-bucket" bind:value={bucket} placeholder="my-bucket" />
			</div>
			<div class="space-y-1">
				<Label for="lance-path">Path (optional)</Label>
				<Input id="lance-path" bind:value={path} placeholder="data/lance" />
			</div>
			<Button class="w-full" onclick={connect} disabled={!bucket}>
				<RefreshCw class="mr-2 h-4 w-4" />
				Load Tables
			</Button>
		</Card.Content>
	</Card.Root>

	{#if selectedBucket && tables.length > 0}
		<Card.Root>
			<Card.Header>
				<Card.Title class="text-sm">Tables ({tables.length})</Card.Title>
			</Card.Header>
			<Card.Content class="space-y-1">
				{#each tables as name (name)}
					<button
						class="flex w-full items-center gap-2 rounded-md px-3 py-2 text-sm transition-colors
							{selectedTable === name ? 'bg-primary text-primary-foreground' : 'hover:bg-muted'}"
						onclick={() => selectTable(name)}
					>
						<Table2 class="h-4 w-4 shrink-0" />
						{name}
					</button>
				{/each}
			</Card.Content>
		</Card.Root>
	{/if}

	{#if selectedTable}
		<Card.Root>
			<Card.Header>
				<div class="flex items-center gap-2">
					<FileType class="h-4 w-4 text-muted-foreground" />
					<Card.Title class="text-sm">Schema</Card.Title>
					<Badge variant="secondary" class="ml-auto">{fields.length} fields</Badge>
				</div>
			</Card.Header>
			<Card.Content>
				{#if fields.length === 0}
					<div class="space-y-2">
						{#each Array(4) as _, i (i)}
							<Skeleton class="h-6 w-full" />
						{/each}
					</div>
				{:else}
					<div class="grid gap-1 font-mono text-xs">
						{#each fields as field (field.name)}
							<div
								class="flex min-w-0 items-center gap-2 rounded px-2 py-1
									{field.is_vector ? 'bg-blue-50 dark:bg-blue-950/20' : ''}"
							>
								<span class="truncate font-medium">{field.name}</span>
								<span class="shrink-0 text-muted-foreground">
									{#if field.is_vector}
										vec[{field.vector_dim ?? '?'}]
									{:else if field.is_binary}
										bin
									{:else}
										{field.type}
									{/if}
								</span>
								{#if field.is_vector}
									<Badge variant="outline" class="ml-auto shrink-0 gap-1 text-xs">
										<Waypoints class="h-3 w-3" />
										vector
									</Badge>
								{:else if field.is_binary}
									<Badge variant="outline" class="ml-auto shrink-0 gap-1 text-xs text-amber-600">
										binary
									</Badge>
								{/if}
							</div>
						{/each}
					</div>
				{/if}
			</Card.Content>
		</Card.Root>
	{/if}

	{#if error}
		<ErrorBanner message={String(error)} />
	{/if}
</div>
