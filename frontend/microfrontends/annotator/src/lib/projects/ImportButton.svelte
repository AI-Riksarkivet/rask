<script lang="ts">
	// Import annotations made elsewhere into ONE task's draft.
	//
	// The file is posted as raw bytes to this zone's `+server.ts` import route, not through a remote
	// function: an Arrow file is exactly the payload kind the estate's transport rule reserves for
	// `+server.ts`, because devalue would carry it as base64 inside the payload string.
	//
	// The refusals are the interesting half. A label outside the task's taxonomy, a relation that is
	// not declared, an id that collides with work already in the draft — each comes back as a 400
	// naming the offender, and those words are shown verbatim. Paraphrasing them into "import failed"
	// would throw away the only thing that lets someone fix the file.
	import { base } from '$app/paths';
	import { Upload } from '@lucide/svelte';
	import { Button } from '@rask/ui/button';

	let {
		taskId,
		onimported,
	}: {
		taskId: string;
		/** Fired after a successful import so the queue can refetch — the draft has changed. */
		onimported?: (count: number) => void;
	} = $props();

	let input = $state<HTMLInputElement | null>(null);
	let busy = $state(false);
	let error = $state('');
	let done = $state('');

	async function send(file: File): Promise<void> {
		busy = true;
		error = '';
		done = '';
		try {
			const response = await fetch(`${base}/api/tasks/${taskId}/import`, {
				method: 'POST',
				headers: { 'content-type': 'application/vnd.apache.arrow.stream' },
				body: await file.arrayBuffer(),
			});
			const body = (await response.json().catch(() => ({}))) as {
				detail?: string;
				imported?: number;
			};
			if (!response.ok) {
				// The server's own words. Every refusal names its offender.
				error = body.detail ?? `HTTP ${response.status}`;
				return;
			}
			const count = body.imported ?? 0;
			done = `${count} imported`;
			onimported?.(count);
		} catch (err) {
			error = String(err);
		} finally {
			busy = false;
		}
	}

	function pick(event: Event): void {
		const chosen = (event.currentTarget as HTMLInputElement).files?.[0];
		// Reset the input's value so choosing the SAME file twice fires `change` again — otherwise a
		// second attempt after a refusal looks like the button stopped working.
		(event.currentTarget as HTMLInputElement).value = '';
		if (chosen) void send(chosen);
	}
</script>

<Button
	variant="outline"
	size="sm"
	disabled={busy}
	data-testid="import-annotations"
	title="import annotations from an Arrow file matching the annotations schema"
	onclick={() => input?.click()}
>
	<Upload class="size-3.5" />
	{busy ? 'Importing…' : 'Import'}
</Button>

<input
	bind:this={input}
	type="file"
	accept=".arrow,.ipc,application/vnd.apache.arrow.stream"
	class="hidden"
	aria-hidden="true"
	tabindex="-1"
	onchange={pick}
/>

{#if error}
	<span class="text-destructive text-xs" data-testid="import-error">{error}</span>
{:else if done}
	<span class="text-muted-foreground text-xs" data-testid="import-done">{done}</span>
{/if}
