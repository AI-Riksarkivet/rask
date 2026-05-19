<script lang="ts">
	import * as Collapsible from '$lib/components/ui/collapsible/index.js';
	import { Input } from '$lib/components/ui/input/index.js';
	import { Label } from '$lib/components/ui/label/index.js';
	import { Badge } from '$lib/components/ui/badge/index.js';
	import { Switch } from '$lib/components/ui/switch/index.js';
	import { Textarea } from '$lib/components/ui/textarea/index.js';
	import { ChevronDown, ChevronRight, Pencil, Code, AlertTriangle } from 'lucide-svelte';
	import { toast } from 'svelte-sonner';
	import { type TemplateNamespace, getTags, PERMISSION_KEYS } from '$lib/types/ns-templates.js';

	let {
		ns,
		index,
		name,
		hasConflict,
		disabled = false,
		onnamechange,
		onnsupdate,
	}: {
		ns: TemplateNamespace;
		index: number;
		name: string;
		hasConflict: boolean;
		disabled?: boolean;
		onnamechange: (index: number, newName: string) => void;
		onnsupdate: (index: number, updated: TemplateNamespace) => void;
	} = $props();

	let expanded = $state(false);
	let rawMode = $state(false);
	let rawJsonText = $state('');

	function summarize(t: TemplateNamespace): string {
		const parts: string[] = [];
		if (t.hashScheme) parts.push(t.hashScheme);
		if (t.hardQuota) parts.push(`${t.hardQuota} quota`);
		if (t.versioningEnabled || t.versioning?.enabled) parts.push('versioning: on');
		if (t.searchEnabled) parts.push('search: on');
		const tagList = getTags(t.tags);
		if (tagList.length) parts.push(`tags: ${tagList.join(', ')}`);
		return parts.join(' | ') || 'default settings';
	}

	function toggleRaw() {
		if (rawMode) {
			try {
				const parsed = JSON.parse(rawJsonText);
				onnsupdate(index, parsed);
				rawMode = false;
			} catch {
				toast.error('Invalid JSON -- fix errors before switching to structured view');
			}
		} else {
			rawJsonText = JSON.stringify(ns, null, 2);
			rawMode = true;
		}
	}

	function updateField(field: keyof TemplateNamespace, value: unknown) {
		const updated = { ...ns };
		(updated as Record<string, unknown>)[field] = value;
		onnsupdate(index, updated);
	}

	function updatePermission(key: string, value: boolean) {
		const updated = { ...ns };
		updated.permissions = { ...(updated.permissions ?? {}), [key]: value };
		onnsupdate(index, updated);
	}

	function updateTags(tagsStr: string) {
		const tagArr = tagsStr
			.split(',')
			.map((t) => t.trim())
			.filter(Boolean);
		updateField('tags', tagArr.length > 0 ? { tag: tagArr } : undefined);
	}
</script>

<div class="space-y-1 rounded-md border p-3" class:border-amber-500={hasConflict}>
	<div class="flex items-center gap-2">
		<div class="flex-1 space-y-1">
			<Label for="import-name-{index}">Name</Label>
			<Input
				id="import-name-{index}"
				value={name}
				oninput={(e) => onnamechange(index, (e.currentTarget as HTMLInputElement).value)}
				placeholder={ns.name}
				{disabled}
			/>
		</div>
	</div>

	{#if hasConflict}
		<div class="flex items-center gap-1.5 pt-1 text-xs text-amber-600">
			<AlertTriangle class="h-3.5 w-3.5 shrink-0" />
			<span>
				A namespace named "{name}" already exists. Import will fail unless you change this name.
			</span>
		</div>
	{/if}

	<p class="text-xs text-muted-foreground">{summarize(ns)}</p>

	{#if !disabled}
		<Collapsible.Root open={expanded} onOpenChange={(v) => (expanded = v)}>
			<Collapsible.Trigger
				class="mt-1 flex items-center gap-1 text-xs font-medium text-primary hover:underline"
			>
				{#if expanded}
					<ChevronDown class="h-3.5 w-3.5" />
				{:else}
					<ChevronRight class="h-3.5 w-3.5" />
				{/if}
				<Pencil class="h-3 w-3" />
				Edit Settings
			</Collapsible.Trigger>
			<Collapsible.Content>
				<div class="mt-2 space-y-3 rounded-md border bg-muted/30 p-3">
					<div class="flex items-center justify-end gap-2">
						<button
							type="button"
							class="flex items-center gap-1 text-xs text-muted-foreground hover:text-foreground"
							onclick={toggleRaw}
						>
							{#if rawMode}
								<Pencil class="h-3 w-3" />
								Structured view
							{:else}
								<Code class="h-3 w-3" />
								Raw JSON
							{/if}
						</button>
					</div>

					{#if rawMode}
						<Textarea
							value={rawJsonText}
							oninput={(e) => (rawJsonText = (e.currentTarget as HTMLTextAreaElement).value)}
							class="min-h-48 font-mono text-xs"
							placeholder="Edit raw JSON..."
						/>
						<p class="text-xs text-muted-foreground">
							Switch back to structured view to apply changes.
						</p>
					{:else}
						<div class="space-y-3">
							<div class="space-y-1">
								<Label for="edit-desc-{index}" class="text-xs">Description</Label>
								<Input
									id="edit-desc-{index}"
									value={ns.description ?? ''}
									oninput={(e) =>
										updateField(
											'description',
											(e.currentTarget as HTMLInputElement).value || undefined
										)}
									placeholder="Optional description"
								/>
							</div>

							<div class="grid grid-cols-2 gap-3">
								<div class="space-y-1">
									<Label for="edit-hard-{index}" class="text-xs">Hard Quota</Label>
									<Input
										id="edit-hard-{index}"
										value={ns.hardQuota ?? ''}
										oninput={(e) =>
											updateField(
												'hardQuota',
												(e.currentTarget as HTMLInputElement).value || undefined
											)}
										placeholder="e.g. 50 GB"
									/>
								</div>
								<div class="space-y-1">
									<Label for="edit-soft-{index}" class="text-xs">Soft Quota (%)</Label>
									<Input
										id="edit-soft-{index}"
										type="number"
										min="10"
										max="95"
										value={ns.softQuota != null ? String(ns.softQuota) : ''}
										oninput={(e) => {
											const val = (e.currentTarget as HTMLInputElement).value;
											updateField('softQuota', val ? Number(val) : undefined);
										}}
										placeholder="85"
									/>
								</div>
							</div>

							<div class="flex flex-wrap gap-x-5 gap-y-2">
								<div class="flex items-center gap-2">
									<Switch
										id="edit-search-{index}"
										checked={ns.searchEnabled ?? false}
										onCheckedChange={(v) => updateField('searchEnabled', v)}
									/>
									<Label for="edit-search-{index}" class="text-xs">Search</Label>
								</div>
								<div class="flex items-center gap-2">
									<Switch
										id="edit-versioning-{index}"
										checked={ns.versioningEnabled ?? ns.versioning?.enabled ?? false}
										onCheckedChange={(v) => {
											const updated = { ...ns, versioningEnabled: v };
											if (ns.versioning) {
												updated.versioning = { ...ns.versioning, enabled: v };
											}
											onnsupdate(index, updated);
										}}
									/>
									<Label for="edit-versioning-{index}" class="text-xs">Versioning</Label>
								</div>
							</div>

							{#if ns.hashScheme}
								<div class="flex items-center gap-2">
									<span class="text-xs text-muted-foreground">Hash Scheme:</span>
									<Badge variant="secondary" class="text-xs">{ns.hashScheme}</Badge>
									<span class="text-xs text-muted-foreground">(read-only, set at creation)</span>
								</div>
							{/if}

							<div class="space-y-1">
								<Label for="edit-tags-{index}" class="text-xs">Tags (comma-separated)</Label>
								<Input
									id="edit-tags-{index}"
									value={getTags(ns.tags).join(', ')}
									oninput={(e) => updateTags((e.currentTarget as HTMLInputElement).value)}
									placeholder="e.g. lakefs, nfs, s3"
								/>
							</div>

							{#if ns.permissions || true}
								<div class="space-y-1.5">
									<span class="text-xs font-medium">Permissions</span>
									<div class="grid grid-cols-2 gap-x-4 gap-y-1.5">
										{#each PERMISSION_KEYS as perm (perm.key)}
											{@const checked =
												(ns.permissions as Record<string, boolean>)?.[perm.key] ?? false}
											<div class="flex items-center gap-2">
												<Switch
													id="edit-perm-{index}-{perm.key}"
													{checked}
													onCheckedChange={(v) => updatePermission(perm.key, v)}
												/>
												<Label for="edit-perm-{index}-{perm.key}" class="text-xs">
													{perm.label}
												</Label>
											</div>
										{/each}
									</div>
								</div>
							{/if}
						</div>
					{/if}
				</div>
			</Collapsible.Content>
		</Collapsible.Root>
	{/if}
</div>
