<script lang="ts">
	import * as Dialog from '$lib/components/ui/dialog/index.js';
	import * as Tooltip from '$lib/components/ui/tooltip/index.js';
	import { Button } from '$lib/components/ui/button/index.js';
	import { Input } from '$lib/components/ui/input/index.js';
	import { Label } from '$lib/components/ui/label/index.js';
	import { Loader2, Copy, Check } from 'lucide-svelte';
	import { toast } from 'svelte-sonner';
	import { useCopyFeedback } from '$lib/utils/use-copy-feedback.svelte.js';
	import { generate_presigned_url } from '$lib/remote/buckets.remote.js';

	let {
		bucket,
		target = $bindable<string | null>(null),
	}: {
		bucket: string;
		target: string | null;
	} = $props();

	const EXPIRY_PRESETS = [
		{ label: '5 minutes', value: 300 },
		{ label: '1 hour', value: 3600 },
		{ label: '6 hours', value: 21600 },
		{ label: '24 hours', value: 86400 },
		{ label: '7 days', value: 604800 },
	];

	let method = $state<'get_object' | 'put_object'>('get_object');
	let expiry = $state(3600);
	let generating = $state(false);
	let url = $state('');
	const { copied, copy: copyToClipboard } = useCopyFeedback();

	function getDisplayName(key: string): string {
		return key.split('/').filter(Boolean).pop() ?? key;
	}

	function close() {
		target = null;
		url = '';
	}

	function reset() {
		method = 'get_object';
		expiry = 3600;
		url = '';
	}

	$effect(() => {
		if (target) reset();
	});

	async function handleGenerate() {
		if (!target) return;
		generating = true;
		url = '';
		try {
			const result = await generate_presigned_url({
				bucket,
				key: target,
				expires_in: expiry,
				method,
			});
			url = result.url;
		} catch {
			toast.error('Failed to generate presigned URL');
		} finally {
			generating = false;
		}
	}

	function copyUrl() {
		copyToClipboard(url);
	}
</script>

<Dialog.Root
	open={target !== null}
	onOpenChange={(open) => {
		if (!open) close();
	}}
>
	<Dialog.Content class="sm:max-w-lg">
		<Dialog.Header>
			<Dialog.Title>Generate Presigned URL</Dialog.Title>
			<Dialog.Description>
				Create a temporary URL for accessing this object without credentials.
			</Dialog.Description>
		</Dialog.Header>
		<div class="space-y-4">
			<div class="rounded-lg bg-muted/50 p-3">
				<div class="grid gap-1 text-sm">
					<span class="text-muted-foreground">Bucket</span>
					<span class="font-mono font-medium">{bucket}</span>
					<span class="mt-1 text-muted-foreground">Key</span>
					<span class="break-all font-mono font-medium">{target ? getDisplayName(target) : ''}</span
					>
				</div>
			</div>

			<div class="space-y-2">
				<Label>Method</Label>
				<div class="flex gap-2">
					<Button
						variant={method === 'get_object' ? 'default' : 'outline'}
						size="sm"
						onclick={() => (method = 'get_object')}
					>
						Download
					</Button>
					<Button
						variant={method === 'put_object' ? 'default' : 'outline'}
						size="sm"
						onclick={() => (method = 'put_object')}
					>
						Upload
					</Button>
				</div>
			</div>

			<div class="space-y-2">
				<Label for="share-expiry">Expiry</Label>
				<div class="flex flex-wrap gap-2">
					{#each EXPIRY_PRESETS as preset (preset.value)}
						<Button
							variant={expiry === preset.value ? 'default' : 'outline'}
							size="sm"
							onclick={() => (expiry = preset.value)}
						>
							{preset.label}
						</Button>
					{/each}
				</div>
			</div>

			{#if !url}
				<Button onclick={handleGenerate} disabled={generating} class="w-full">
					{#if generating}
						<Loader2 class="h-4 w-4 animate-spin" />
						Generating...
					{:else}
						Generate URL
					{/if}
				</Button>
			{:else}
				<div class="space-y-2">
					<Label>Presigned URL</Label>
					<div class="flex items-center gap-2">
						<Input readonly value={url} class="font-mono text-xs" />
						<Tooltip.Root>
							<Tooltip.Trigger>
								{#snippet child({ props })}
									<Button {...props} variant="ghost" size="icon" onclick={copyUrl}>
										{#if copied}
											<Check class="h-4 w-4 text-emerald-500" />
										{:else}
											<Copy class="h-4 w-4" />
										{/if}
									</Button>
								{/snippet}
							</Tooltip.Trigger>
							<Tooltip.Content>{copied ? 'Copied!' : 'Copy'}</Tooltip.Content>
						</Tooltip.Root>
					</div>
				</div>
			{/if}
		</div>
		<Dialog.Footer>
			<Button variant="ghost" onclick={close}>Close</Button>
		</Dialog.Footer>
	</Dialog.Content>
</Dialog.Root>
