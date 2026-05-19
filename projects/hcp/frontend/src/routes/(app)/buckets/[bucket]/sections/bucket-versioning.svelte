<script lang="ts">
	import * as Tooltip from '$lib/components/ui/tooltip/index.js';
	import { Badge } from '$lib/components/ui/badge/index.js';
	import { Button } from '$lib/components/ui/button/index.js';
	import { Loader2, HelpCircle } from 'lucide-svelte';
	import { toast } from 'svelte-sonner';
	import {
		get_bucket_versioning,
		set_bucket_versioning,
		type BucketVersioning,
	} from '$lib/remote/buckets.remote.js';
	import { getErrorMessage } from '$lib/utils/get-error-message.js';

	let {
		bucket,
	}: {
		bucket: string;
	} = $props();

	let versioningData = $derived(get_bucket_versioning({ bucket }));
	let versioning = $derived((versioningData?.current ?? {}) as BucketVersioning);

	let saving = $state(false);

	async function toggle() {
		if (!versioningData) return;
		const newStatus = versioning.status === 'Enabled' ? 'Suspended' : 'Enabled';
		saving = true;
		try {
			await set_bucket_versioning({ bucket, status: newStatus }).updates(versioningData);
			toast.success(`Versioning ${newStatus.toLowerCase()}`);
		} catch (err) {
			toast.error(getErrorMessage(err, 'Failed to update versioning'));
		} finally {
			saving = false;
		}
	}

	let statusVariant = $derived.by((): 'default' | 'secondary' | 'outline' => {
		if (versioning.status === 'Enabled') return 'default';
		if (versioning.status === 'Suspended') return 'secondary';
		return 'outline';
	});

	let statusLabel = $derived(versioning.status ?? 'Not set');

	let statusTooltip = $derived.by(() => {
		if (versioning.status === 'Enabled')
			return 'Versioning is active — every update or delete creates a new version. Previous versions are preserved.';
		if (versioning.status === 'Suspended')
			return 'Versioning is suspended — new objects will not be versioned, but existing versions are still accessible.';
		return 'Versioning has never been enabled on this bucket. Objects are overwritten on update and permanently deleted.';
	});
</script>

<div class="flex items-center gap-2">
	{#await versioningData}
		<div class="h-5 w-16 animate-pulse rounded bg-muted"></div>
	{:then}
		{#if versioning.error}
			<Badge variant="outline" class="text-amber-600 dark:text-amber-400">Versioning: N/A</Badge>
		{:else}
			<Tooltip.Root>
				<Tooltip.Trigger>
					{#snippet child({ props })}
						<span {...props} class="inline-flex items-center gap-1">
							<Badge variant={statusVariant}>
								Versioning: {statusLabel}
							</Badge>
							<HelpCircle class="h-3 w-3 text-muted-foreground" />
						</span>
					{/snippet}
				</Tooltip.Trigger>
				<Tooltip.Content side="bottom" class="max-w-xs">{statusTooltip}</Tooltip.Content>
			</Tooltip.Root>
			<Button variant="ghost" size="sm" class="h-7 px-2 text-xs" onclick={toggle} disabled={saving}>
				{#if saving}
					<Loader2 class="h-3 w-3 animate-spin" />
				{:else if versioning.status === 'Enabled'}
					Suspend
				{:else}
					Enable
				{/if}
			</Button>
		{/if}
	{/await}
</div>
