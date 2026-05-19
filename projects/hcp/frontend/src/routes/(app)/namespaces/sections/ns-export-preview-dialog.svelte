<script lang="ts">
	import { SvelteSet } from 'svelte/reactivity';
	import * as Dialog from '$lib/components/ui/dialog/index.js';
	import { Button } from '$lib/components/ui/button/index.js';
	import { Badge } from '$lib/components/ui/badge/index.js';
	import { Label } from '$lib/components/ui/label/index.js';
	import { Switch } from '$lib/components/ui/switch/index.js';
	import { Download, Loader2, ChevronDown, ChevronRight } from 'lucide-svelte';
	import { type TemplateNamespace, getTags } from '$lib/types/ns-templates.js';

	let {
		open = $bindable(false),
		data,
		loading = false,
		ondownload,
	}: {
		open: boolean;
		data: Record<string, unknown> | null;
		loading?: boolean;
		ondownload: () => void;
	} = $props();

	let showRaw = $state(false);
	let expandedNs = new SvelteSet<string>();

	let namespaces = $derived.by<TemplateNamespace[]>(() => {
		if (!data) return [];
		if (data.namespaces && Array.isArray(data.namespaces)) {
			return data.namespaces as TemplateNamespace[];
		}
		if (Array.isArray(data)) {
			return data as TemplateNamespace[];
		}
		return [data as unknown as TemplateNamespace];
	});

	function toggleNs(name: string) {
		if (expandedNs.has(name)) expandedNs.delete(name);
		else expandedNs.add(name);
	}

	// Reset local state when dialog opens
	$effect(() => {
		if (open) {
			showRaw = false;
			expandedNs.clear();
		}
	});
</script>

<Dialog.Root {open} onOpenChange={(v) => (open = v)}>
	<Dialog.Content class="sm:max-w-2xl">
		<Dialog.Header>
			<Dialog.Title>Export Preview</Dialog.Title>
			<Dialog.Description>Review the exported configuration before downloading.</Dialog.Description>
		</Dialog.Header>

		{#if loading}
			<div class="flex items-center justify-center py-12">
				<Loader2 class="h-6 w-6 animate-spin text-muted-foreground" />
				<span class="ml-2 text-sm text-muted-foreground">Loading export data...</span>
			</div>
		{:else if data}
			<div class="space-y-3">
				<div class="flex items-center justify-end gap-2">
					<Label for="preview-raw-toggle" class="text-xs text-muted-foreground">Raw JSON</Label>
					<Switch
						id="preview-raw-toggle"
						checked={showRaw}
						onCheckedChange={(v) => (showRaw = v)}
					/>
				</div>

				{#if showRaw}
					<div class="max-h-96 overflow-auto rounded-md border bg-muted/50 p-3">
						<pre class="whitespace-pre-wrap text-xs">{JSON.stringify(data, null, 2)}</pre>
					</div>
				{:else}
					<div class="max-h-96 space-y-2 overflow-y-auto">
						{#each namespaces as ns (ns.name)}
							{@const expanded = expandedNs.has(ns.name)}
							<div class="rounded-md border">
								<button
									type="button"
									class="flex w-full items-center gap-2 p-3 text-left hover:bg-muted/50"
									onclick={() => toggleNs(ns.name)}
								>
									{#if expanded}
										<ChevronDown class="h-4 w-4 shrink-0 text-muted-foreground" />
									{:else}
										<ChevronRight class="h-4 w-4 shrink-0 text-muted-foreground" />
									{/if}
									<span class="text-sm font-medium">{ns.name}</span>
									{#if ns.hashScheme}
										<Badge variant="secondary" class="text-xs">{ns.hashScheme}</Badge>
									{/if}
									{#if ns.hardQuota}
										<Badge variant="outline" class="text-xs">{ns.hardQuota}</Badge>
									{/if}
								</button>

								{#if expanded}
									<div class="space-y-2 border-t px-3 pb-3 pt-2">
										{#if ns.description}
											<div class="text-xs">
												<span class="font-medium text-muted-foreground">Description:</span>
												{ns.description}
											</div>
										{/if}

										<div class="flex flex-wrap gap-1.5">
											{#if ns.searchEnabled}
												<Badge variant="secondary" class="text-xs">Search: on</Badge>
											{/if}
											{#if ns.versioningEnabled || ns.versioning?.enabled}
												<Badge variant="secondary" class="text-xs">Versioning: on</Badge>
											{/if}
											{#if ns.softQuota != null}
												<Badge variant="outline" class="text-xs">
													Soft quota: {ns.softQuota}%
												</Badge>
											{/if}
										</div>

										{#if getTags(ns.tags).length}
											<div class="flex flex-wrap gap-1">
												<span class="text-xs font-medium text-muted-foreground">Tags:</span>
												{#each getTags(ns.tags) as tag (tag)}
													<Badge variant="outline" class="text-xs">{tag}</Badge>
												{/each}
											</div>
										{/if}

										{#if ns.permissions}
											<div>
												<span class="text-xs font-medium text-muted-foreground">
													Permissions:
												</span>
												<div class="mt-1 flex flex-wrap gap-1">
													{#each Object.entries(ns.permissions) as [key, val] (key)}
														<Badge variant={val ? 'secondary' : 'outline'} class="text-xs">
															{key.replace('Allowed', '')}: {val ? 'yes' : 'no'}
														</Badge>
													{/each}
												</div>
											</div>
										{/if}

										{#if ns.compliance}
											<div>
												<span class="text-xs font-medium text-muted-foreground"> Compliance: </span>
												<div class="mt-1 flex flex-wrap gap-1">
													{#each Object.entries(ns.compliance) as [key, val] (key)}
														<Badge variant="outline" class="text-xs">
															{key}: {val}
														</Badge>
													{/each}
												</div>
											</div>
										{/if}

										{#if ns.retentionClasses?.length}
											<div>
												<span class="text-xs font-medium text-muted-foreground">
													Retention Classes ({ns.retentionClasses.length}):
												</span>
												<div class="mt-1 flex flex-wrap gap-1">
													{#each ns.retentionClasses as rc (rc.name)}
														<Badge variant="outline" class="text-xs">
															{rc.name} = {rc.value}
														</Badge>
													{/each}
												</div>
											</div>
										{/if}

										{#if ns.protocols}
											<div>
												<span class="text-xs font-medium text-muted-foreground"> Protocols: </span>
												<div class="mt-1 flex flex-wrap gap-1">
													{#each Object.keys(ns.protocols) as proto (proto)}
														<Badge variant="secondary" class="text-xs">
															{proto.toUpperCase()}
														</Badge>
													{/each}
												</div>
											</div>
										{/if}
									</div>
								{/if}
							</div>
						{/each}
					</div>
				{/if}
			</div>

			<Dialog.Footer>
				<Button variant="ghost" onclick={() => (open = false)}>Close</Button>
				<Button onclick={ondownload}>
					<Download class="h-4 w-4" />
					Download
				</Button>
			</Dialog.Footer>
		{/if}
	</Dialog.Content>
</Dialog.Root>
