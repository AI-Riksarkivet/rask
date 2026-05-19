<script lang="ts">
	import { SvelteSet } from 'svelte/reactivity';
	import * as Card from '$lib/components/ui/card/index.js';
	import { Button } from '$lib/components/ui/button/index.js';
	import { Input } from '$lib/components/ui/input/index.js';
	import { Checkbox } from '$lib/components/ui/checkbox/index.js';
	import { toast } from 'svelte-sonner';
	import { Download, Search, Eye, Loader2 } from 'lucide-svelte';
	import { export_namespace_configs, type Namespace } from '$lib/remote/namespaces.remote.js';
	import NsExportPreviewDialog from './ns-export-preview-dialog.svelte';
	import { getErrorMessage } from '$lib/utils/get-error-message.js';

	let {
		tenant,
		namespaces,
	}: {
		tenant: string;
		namespaces: Namespace[];
	} = $props();

	let searchFilter = $state('');
	let selected = new SvelteSet<string>();
	let exporting = $state(false);

	let previewOpen = $state(false);
	let previewData = $state<Record<string, unknown> | null>(null);
	let previewLoading = $state(false);

	let filteredNamespaces = $derived(
		namespaces.filter((ns) => ns.name.toLowerCase().includes(searchFilter.toLowerCase()))
	);

	let allFilteredSelected = $derived(
		filteredNamespaces.length > 0 && filteredNamespaces.every((ns) => selected.has(ns.name))
	);

	function toggleAll(checked: boolean) {
		for (const ns of filteredNamespaces) {
			if (checked) selected.add(ns.name);
			else selected.delete(ns.name);
		}
	}

	function toggleNamespace(name: string, checked: boolean) {
		if (checked) selected.add(name);
		else selected.delete(name);
	}

	async function handleExport() {
		if (selected.size === 0) return;
		exporting = true;
		try {
			const result = await export_namespace_configs({ tenant, names: [...selected] });
			const blob = new Blob([JSON.stringify(result, null, 2)], { type: 'application/json' });
			const url = URL.createObjectURL(blob);
			const a = document.createElement('a');
			a.href = url;
			a.download = `namespace-templates-${new Date().toISOString().slice(0, 10)}.json`;
			a.click();
			URL.revokeObjectURL(url);
			toast.success(`Exported ${selected.size} namespace template(s)`);
		} catch (err) {
			toast.error(getErrorMessage(err, 'Export failed'));
		} finally {
			exporting = false;
		}
	}

	async function handlePreview() {
		if (selected.size === 0) return;
		previewLoading = true;
		previewData = null;
		previewOpen = true;
		try {
			const result = await export_namespace_configs({ tenant, names: [...selected] });
			previewData = result as Record<string, unknown>;
		} catch (err) {
			toast.error(getErrorMessage(err, 'Failed to load preview'));
			previewOpen = false;
		} finally {
			previewLoading = false;
		}
	}

	function downloadPreview() {
		if (!previewData) return;
		const blob = new Blob([JSON.stringify(previewData, null, 2)], { type: 'application/json' });
		const url = URL.createObjectURL(blob);
		const a = document.createElement('a');
		a.href = url;
		a.download = `namespace-templates-${new Date().toISOString().slice(0, 10)}.json`;
		a.click();
		URL.revokeObjectURL(url);
		toast.success(`Downloaded ${selected.size} namespace template(s)`);
		previewOpen = false;
	}
</script>

<NsExportPreviewDialog
	bind:open={previewOpen}
	data={previewData}
	loading={previewLoading}
	ondownload={downloadPreview}
/>

<Card.Root>
	<Card.Header>
		<div class="flex items-center gap-2">
			<Download class="h-5 w-5 text-muted-foreground" />
			<div>
				<Card.Title>Export Templates</Card.Title>
				<Card.Description>Export namespace configurations as JSON templates</Card.Description>
			</div>
		</div>
	</Card.Header>
	<Card.Content class="space-y-4">
		<div class="relative">
			<Search class="absolute left-3 top-1/2 h-4 w-4 -translate-y-1/2 text-muted-foreground" />
			<Input bind:value={searchFilter} placeholder="Filter namespaces..." class="pl-10" />
		</div>

		<div class="max-h-72 space-y-1 overflow-y-auto rounded-md border p-3">
			<div class="mb-1 flex items-center gap-2 border-b pb-2">
				<Checkbox checked={allFilteredSelected} onCheckedChange={(val) => toggleAll(!!val)} />
				<span class="text-sm font-medium">
					{allFilteredSelected ? 'Deselect all' : 'Select all'}
					{searchFilter ? ` matching (${filteredNamespaces.length})` : ` (${namespaces.length})`}
				</span>
			</div>
			{#each filteredNamespaces as ns (ns.name)}
				<div class="flex items-center gap-2">
					<Checkbox
						checked={selected.has(ns.name)}
						onCheckedChange={(val) => toggleNamespace(ns.name, !!val)}
					/>
					<span class="text-sm">{ns.name}</span>
				</div>
			{/each}
			{#if filteredNamespaces.length === 0}
				<p class="py-2 text-center text-sm text-muted-foreground">No namespaces found</p>
			{/if}
		</div>

		<div class="flex items-center justify-between">
			<p class="text-sm text-muted-foreground">{selected.size} selected</p>
			<div class="flex gap-2">
				<Button
					variant="outline"
					onclick={handlePreview}
					disabled={selected.size === 0 || previewLoading}
				>
					{#if previewLoading}
						<Loader2 class="h-4 w-4 animate-spin" />
						Loading...
					{:else}
						<Eye class="h-4 w-4" />
						Preview
					{/if}
				</Button>
				<Button onclick={handleExport} disabled={selected.size === 0 || exporting}>
					{#if exporting}
						<Loader2 class="h-4 w-4 animate-spin" />
						Exporting...
					{:else}
						<Download class="h-4 w-4" />
						Export Selected
					{/if}
				</Button>
			</div>
		</div>
	</Card.Content>
</Card.Root>
