<script lang="ts">
	import * as Dialog from '$lib/components/ui/dialog/index.js';
	import { Button } from '$lib/components/ui/button/index.js';
	import { Loader2 } from 'lucide-svelte';
	import ErrorBanner from '$lib/components/custom/error-banner/error-banner.svelte';
	import type { Snippet } from 'svelte';

	let {
		open = $bindable(false),
		title,
		description = '',
		submitLabel = 'Create',
		loading = false,
		error = '',
		onsubmit,
		class: className = 'sm:max-w-lg',
		children,
	}: {
		open: boolean;
		title: string;
		description?: string;
		submitLabel?: string;
		loading?: boolean;
		error?: string;
		onsubmit: (e: SubmitEvent) => void;
		class?: string;
		children: Snippet;
	} = $props();
</script>

<Dialog.Root bind:open>
	<Dialog.Content class={className}>
		<Dialog.Header>
			<Dialog.Title>{title}</Dialog.Title>
			{#if description}
				<Dialog.Description>{description}</Dialog.Description>
			{/if}
		</Dialog.Header>
		<form {onsubmit} class="space-y-4">
			<ErrorBanner message={error} />
			{@render children()}
			<Dialog.Footer>
				<Button variant="ghost" type="button" onclick={() => (open = false)}>Cancel</Button>
				<Button type="submit" disabled={loading}>
					{#if loading}
						<Loader2 class="h-4 w-4 animate-spin" />
						{submitLabel}...
					{:else}
						{submitLabel}
					{/if}
				</Button>
			</Dialog.Footer>
		</form>
	</Dialog.Content>
</Dialog.Root>
