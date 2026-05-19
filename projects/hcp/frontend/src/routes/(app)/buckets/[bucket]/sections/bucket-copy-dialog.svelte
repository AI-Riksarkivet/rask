<script lang="ts">
	import * as Dialog from '$lib/components/ui/dialog/index.js';
	import { Button } from '$lib/components/ui/button/index.js';
	import { Input } from '$lib/components/ui/input/index.js';
	import { Label } from '$lib/components/ui/label/index.js';
	import { Loader2 } from 'lucide-svelte';
	import { toast } from 'svelte-sonner';
	import { copy_object, get_buckets } from '$lib/remote/buckets.remote.js';
	import { getErrorMessage } from '$lib/utils/get-error-message.js';

	let {
		bucket,
		target = $bindable<string | null>(null),
		oncopied,
	}: {
		bucket: string;
		target: string | null;
		oncopied: () => void;
	} = $props();

	let destBucket = $state('');
	let destKey = $state('');
	let copying = $state(false);

	let bucketsData = get_buckets();
	let buckets = $derived(
		(bucketsData.current?.buckets ?? []) as { name: string; creation_date: string }[]
	);

	function getDisplayName(key: string): string {
		return key.split('/').filter(Boolean).pop() ?? key;
	}

	$effect(() => {
		if (target) {
			destBucket = bucket;
			destKey = target;
		}
	});

	function close() {
		target = null;
	}

	async function handleCopy() {
		if (!target || !destBucket || !destKey) return;
		copying = true;
		try {
			await copy_object({
				bucket: destBucket,
				key: destKey,
				source_bucket: bucket,
				source_key: target,
			});
			toast.success(`Copied to ${destBucket}/${getDisplayName(destKey)}`);
			if (destBucket === bucket) oncopied();
			close();
		} catch (err) {
			toast.error(getErrorMessage(err, 'Failed to copy object'));
		} finally {
			copying = false;
		}
	}
</script>

<Dialog.Root
	open={target !== null}
	onOpenChange={(open) => {
		if (!open) close();
	}}
>
	<Dialog.Content class="sm:max-w-md">
		<Dialog.Header>
			<Dialog.Title>Copy Object</Dialog.Title>
			<Dialog.Description>
				Create a server-side copy of this object. The copy is independent — changes to one do not
				affect the other.
			</Dialog.Description>
		</Dialog.Header>

		<div class="space-y-4">
			<div class="rounded-lg bg-muted/50 p-3">
				<div class="grid gap-1 text-sm">
					<span class="text-muted-foreground">Source</span>
					<span class="break-all font-mono font-medium">{bucket}/{target}</span>
				</div>
			</div>

			<div class="space-y-2">
				<Label for="dest-bucket">Destination Bucket</Label>
				<select
					id="dest-bucket"
					class="border-input bg-background text-foreground ring-offset-background focus:ring-ring flex h-9 w-full items-center rounded-md border px-3 py-2 text-sm shadow-sm focus:outline-none focus:ring-1 disabled:cursor-not-allowed disabled:opacity-50"
					bind:value={destBucket}
				>
					{#each buckets as b (b.name)}
						<option value={b.name}>{b.name}</option>
					{/each}
				</select>
				<p class="text-xs text-muted-foreground">
					The bucket where the copy will be created. Can be the same or a different bucket.
				</p>
			</div>

			<div class="space-y-2">
				<Label for="dest-key">Destination Key</Label>
				<Input id="dest-key" bind:value={destKey} class="font-mono text-sm" />
				<p class="text-xs text-muted-foreground">
					The full object path including any prefix (folder). Change the name to avoid overwriting
					the original.
				</p>
			</div>
		</div>

		<Dialog.Footer>
			<Button variant="ghost" onclick={close} disabled={copying}>Cancel</Button>
			<Button onclick={handleCopy} disabled={copying || !destBucket || !destKey}>
				{#if copying}
					<Loader2 class="h-4 w-4 animate-spin" />
					Copying...
				{:else}
					Copy
				{/if}
			</Button>
		</Dialog.Footer>
	</Dialog.Content>
</Dialog.Root>
