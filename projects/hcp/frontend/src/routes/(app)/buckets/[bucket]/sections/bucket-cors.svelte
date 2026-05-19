<script lang="ts">
	import * as Card from '$lib/components/ui/card/index.js';
	import { Button } from '$lib/components/ui/button/index.js';
	import { Input } from '$lib/components/ui/input/index.js';
	import { Label } from '$lib/components/ui/label/index.js';
	import SaveButton from '$lib/components/custom/save-button/save-button.svelte';
	import DeleteConfirmDialog from '$lib/components/custom/delete-confirm-dialog/delete-confirm-dialog.svelte';
	import ErrorBanner from '$lib/components/custom/error-banner/error-banner.svelte';
	import { useSave } from '$lib/utils/use-save.svelte.js';
	import {
		get_bucket_cors,
		put_bucket_cors,
		delete_bucket_cors,
		type CorsRule,
	} from '$lib/remote/buckets.remote.js';
	import { Plus, Trash2, Globe, X } from 'lucide-svelte';
	import { toast } from 'svelte-sonner';
	import { getErrorMessage } from '$lib/utils/get-error-message.js';

	const HTTP_METHODS = ['GET', 'PUT', 'POST', 'DELETE', 'HEAD'] as const;

	let {
		bucket,
	}: {
		bucket: string;
	} = $props();

	let corsData = $derived(get_bucket_cors({ bucket }));
	let cors = $derived(
		(corsData?.current ?? { cors_rules: [], error: null }) as {
			cors_rules: CorsRule[];
			error: string | null;
		}
	);

	const saver = useSave({
		successMsg: 'CORS configuration saved',
		errorMsg: 'Failed to save CORS configuration',
	});

	let localRules = $state<CorsRule[]>([]);
	let deleteOpen = $state(false);
	let deleting = $state(false);

	// Sync server data into local state
	$effect(() => {
		const c = cors;
		void saver.syncVersion;
		localRules = structuredClone(c.cors_rules);
	});

	let dirty = $derived.by(() => {
		return JSON.stringify(localRules) !== JSON.stringify(cors.cors_rules);
	});

	function addRule() {
		localRules = [
			...localRules,
			{
				AllowedOrigins: ['*'],
				AllowedMethods: ['GET'],
				AllowedHeaders: ['*'],
				ExposeHeaders: [],
				MaxAgeSeconds: 3600,
			},
		];
	}

	function removeRule(index: number) {
		localRules = localRules.filter((_, i) => i !== index);
	}

	function toggleMethod(ruleIndex: number, method: string) {
		const rule = localRules[ruleIndex];
		const methods = rule.AllowedMethods ?? [];
		if (methods.includes(method)) {
			localRules[ruleIndex] = {
				...rule,
				AllowedMethods: methods.filter((m) => m !== method),
			};
		} else {
			localRules[ruleIndex] = {
				...rule,
				AllowedMethods: [...methods, method],
			};
		}
	}

	function addTag(
		ruleIndex: number,
		field: 'AllowedOrigins' | 'AllowedHeaders' | 'ExposeHeaders',
		value: string
	) {
		const trimmed = value.trim();
		if (!trimmed) return;
		const rule = localRules[ruleIndex];
		const current = rule[field] ?? [];
		if (current.includes(trimmed)) return;
		localRules[ruleIndex] = {
			...rule,
			[field]: [...current, trimmed],
		};
	}

	function removeTag(
		ruleIndex: number,
		field: 'AllowedOrigins' | 'AllowedHeaders' | 'ExposeHeaders',
		tagIndex: number
	) {
		const rule = localRules[ruleIndex];
		const current = rule[field] ?? [];
		localRules[ruleIndex] = {
			...rule,
			[field]: current.filter((_, i) => i !== tagIndex),
		};
	}

	function handleTagKeydown(
		event: KeyboardEvent,
		ruleIndex: number,
		field: 'AllowedOrigins' | 'AllowedHeaders' | 'ExposeHeaders'
	) {
		if (event.key === 'Enter') {
			event.preventDefault();
			const input = event.target as HTMLInputElement;
			addTag(ruleIndex, field, input.value);
			input.value = '';
		}
	}

	function setMaxAge(ruleIndex: number, value: string) {
		const num = parseInt(value, 10);
		localRules[ruleIndex] = {
			...localRules[ruleIndex],
			MaxAgeSeconds: isNaN(num) ? undefined : num,
		};
	}

	async function deleteAllCors() {
		deleting = true;
		try {
			if (!corsData) return;
			await delete_bucket_cors({ bucket }).updates(corsData);
			toast.success('CORS configuration deleted');
			deleteOpen = false;
		} catch (err) {
			toast.error(getErrorMessage(err, 'Failed to delete CORS configuration'));
		} finally {
			deleting = false;
		}
	}
</script>

<Card.Root>
	<Card.Header class="pb-3">
		<div class="flex items-center justify-between">
			<div class="flex items-center gap-2">
				<Globe class="h-4 w-4 text-muted-foreground" />
				<Card.Title class="text-base">CORS Configuration</Card.Title>
			</div>
			{#if cors.cors_rules.length > 0 && !cors.error}
				<Button
					variant="destructive"
					size="sm"
					class="h-7 text-xs"
					onclick={() => (deleteOpen = true)}
				>
					<Trash2 class="h-3.5 w-3.5" />
					Delete All
				</Button>
			{/if}
		</div>
		<Card.Description>
			Cross-Origin Resource Sharing rules control which web origins can access objects in this
			bucket.
		</Card.Description>
	</Card.Header>
	{#await corsData}
		<Card.Content>
			<div class="space-y-2">
				{#each Array(3) as _, i (i)}
					<div class="h-5 w-48 animate-pulse rounded bg-muted"></div>
				{/each}
			</div>
		</Card.Content>
	{:then}
		<Card.Content class="space-y-4">
			{#if cors.error}
				<ErrorBanner message={cors.error} />
			{:else if localRules.length === 0}
				<div class="rounded-md border border-dashed px-3 py-6 text-center">
					<p class="mb-3 text-sm text-muted-foreground">
						No CORS rules configured. Add a rule to allow cross-origin requests.
					</p>
					<Button variant="outline" size="sm" onclick={addRule}>
						<Plus class="h-4 w-4" />
						Add CORS Rule
					</Button>
				</div>
			{:else}
				<div class="space-y-4">
					{#each localRules as rule, ruleIndex (ruleIndex)}
						<div class="rounded-md border p-4 space-y-3">
							<div class="flex items-center justify-between">
								<span class="text-sm font-medium">Rule {ruleIndex + 1}</span>
								<Button
									variant="ghost"
									size="sm"
									class="h-7 w-7 p-0 text-muted-foreground hover:text-destructive"
									onclick={() => removeRule(ruleIndex)}
								>
									<Trash2 class="h-3.5 w-3.5" />
								</Button>
							</div>

							<!-- Allowed Origins -->
							<div class="space-y-1.5">
								<Label class="text-xs text-muted-foreground">Allowed Origins</Label>
								<div class="flex flex-wrap gap-1.5">
									{#each rule.AllowedOrigins ?? [] as origin, tagIndex (tagIndex)}
										<span
											class="inline-flex items-center gap-1 rounded-md bg-secondary px-2 py-0.5 text-xs font-medium"
										>
											{origin}
											<button
												class="ml-0.5 inline-flex h-3.5 w-3.5 items-center justify-center rounded-full hover:bg-black/20 dark:hover:bg-white/20"
												onclick={() => removeTag(ruleIndex, 'AllowedOrigins', tagIndex)}
											>
												<X class="h-2.5 w-2.5" />
											</button>
										</span>
									{/each}
								</div>
								<Input
									placeholder="e.g. https://example.com (Enter to add)"
									class="h-8 text-sm"
									onkeydown={(e) => handleTagKeydown(e, ruleIndex, 'AllowedOrigins')}
								/>
							</div>

							<!-- Allowed Methods -->
							<div class="space-y-1.5">
								<Label class="text-xs text-muted-foreground">Allowed Methods</Label>
								<div class="flex flex-wrap gap-1.5">
									{#each HTTP_METHODS as method (method)}
										{@const active = (rule.AllowedMethods ?? []).includes(method)}
										<button
											class="rounded-md border px-2.5 py-1 text-xs font-medium transition-colors {active
												? 'border-primary bg-primary text-primary-foreground'
												: 'border-input bg-background text-muted-foreground hover:bg-accent hover:text-accent-foreground'}"
											onclick={() => toggleMethod(ruleIndex, method)}
										>
											{method}
										</button>
									{/each}
								</div>
							</div>

							<!-- Allowed Headers -->
							<div class="space-y-1.5">
								<Label class="text-xs text-muted-foreground">Allowed Headers</Label>
								<div class="flex flex-wrap gap-1.5">
									{#each rule.AllowedHeaders ?? [] as header, tagIndex (tagIndex)}
										<span
											class="inline-flex items-center gap-1 rounded-md bg-secondary px-2 py-0.5 text-xs font-medium"
										>
											{header}
											<button
												class="ml-0.5 inline-flex h-3.5 w-3.5 items-center justify-center rounded-full hover:bg-black/20 dark:hover:bg-white/20"
												onclick={() => removeTag(ruleIndex, 'AllowedHeaders', tagIndex)}
											>
												<X class="h-2.5 w-2.5" />
											</button>
										</span>
									{/each}
								</div>
								<Input
									placeholder="e.g. Content-Type (Enter to add)"
									class="h-8 text-sm"
									onkeydown={(e) => handleTagKeydown(e, ruleIndex, 'AllowedHeaders')}
								/>
							</div>

							<!-- Expose Headers -->
							<div class="space-y-1.5">
								<Label class="text-xs text-muted-foreground">Expose Headers</Label>
								<div class="flex flex-wrap gap-1.5">
									{#each rule.ExposeHeaders ?? [] as header, tagIndex (tagIndex)}
										<span
											class="inline-flex items-center gap-1 rounded-md bg-secondary px-2 py-0.5 text-xs font-medium"
										>
											{header}
											<button
												class="ml-0.5 inline-flex h-3.5 w-3.5 items-center justify-center rounded-full hover:bg-black/20 dark:hover:bg-white/20"
												onclick={() => removeTag(ruleIndex, 'ExposeHeaders', tagIndex)}
											>
												<X class="h-2.5 w-2.5" />
											</button>
										</span>
									{/each}
								</div>
								<Input
									placeholder="e.g. ETag (Enter to add)"
									class="h-8 text-sm"
									onkeydown={(e) => handleTagKeydown(e, ruleIndex, 'ExposeHeaders')}
								/>
							</div>

							<!-- Max Age Seconds -->
							<div class="space-y-1.5">
								<Label class="text-xs text-muted-foreground">Max Age (seconds)</Label>
								<Input
									type="number"
									class="h-8 w-32 text-sm"
									value={rule.MaxAgeSeconds ?? ''}
									oninput={(e) => setMaxAge(ruleIndex, (e.target as HTMLInputElement).value)}
								/>
							</div>
						</div>
					{/each}
				</div>

				<div class="flex items-center justify-between pt-2">
					<Button variant="outline" size="sm" onclick={addRule}>
						<Plus class="h-4 w-4" />
						Add Rule
					</Button>
					<SaveButton
						{dirty}
						saving={saver.saving}
						onclick={() =>
							saver.run(async () => {
								if (!corsData) return;
								await put_bucket_cors({
									bucket,
									cors_rules: localRules,
								}).updates(corsData);
							})}
					/>
				</div>
			{/if}
		</Card.Content>
	{/await}
</Card.Root>

<DeleteConfirmDialog
	bind:open={deleteOpen}
	name={bucket}
	itemType="CORS configuration"
	description="Are you sure you want to delete all CORS rules for this bucket? Cross-origin requests will be blocked."
	loading={deleting}
	onconfirm={deleteAllCors}
/>
