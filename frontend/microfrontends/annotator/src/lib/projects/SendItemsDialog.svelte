<script lang="ts">
	// The v1 send surface: keys pasted or carried over from the corpus browser/search. The richer
	// send-from-search flow lives in the media zone and lands later; this dialog is the honest
	// minimum that makes the loop drivable end to end.
	import { Button } from '@rask/ui/button';
	import { Dialog } from '@rask/ui/dialog';
	import { Textarea } from '@rask/ui/textarea';

	import { sendItems } from './remote/projects.remote';

	let {
		projectId,
		open = $bindable(false),
		onsent,
	}: { projectId: string; open?: boolean; onsent: () => void } = $props();

	let keysText = $state('');
	let sending = $state(false);
	let error = $state('');

	const KEYS_PLACEHOLDER = 'a1b2c3…/0/17\na1b2c3…/0/18';

	async function send(): Promise<void> {
		const keys = keysText
			.split('\n')
			.map((k) => k.trim())
			.filter(Boolean);
		if (keys.length === 0 || sending) return;
		sending = true;
		error = '';
		const result = await sendItems({
			projectId,
			items: keys.map((key) => ({
				source: { kind: 'chunks', keys: [key] },
				media: { kind: 'image' },
			})),
		});
		sending = false;
		if (result.ok) {
			open = false;
			keysText = '';
			onsent();
		} else {
			error = result.detail;
		}
	}
</script>

<Dialog.Root bind:open>
	<Dialog.Content class="sm:max-w-md">
		<Dialog.Title>Send items into this labeling task</Dialog.Title>
		<Dialog.Description>
			One key per line (<span class="font-mono">doc/speech/chunk</span>) — from the corpus browser,
			a search, or an atlas selection. Each becomes a claimable item.
		</Dialog.Description>
		<form
			class="flex flex-col gap-3"
			onsubmit={(e) => {
				e.preventDefault();
				void send();
			}}
		>
			<Textarea
				bind:value={keysText}
				rows={6}
				class="font-mono text-xs"
				placeholder={KEYS_PLACEHOLDER}
			/>
			{#if error}
				<p class="text-destructive text-sm">{error}</p>
			{/if}
			<div class="flex justify-end gap-2">
				<Button type="button" variant="outline" onclick={() => (open = false)}>Cancel</Button>
				<Button type="submit" disabled={sending || !keysText.trim()}>
					{sending ? 'Sending…' : 'Send items'}
				</Button>
			</div>
		</form>
	</Dialog.Content>
</Dialog.Root>
