<script lang="ts">
	import { Button } from '$lib/components/ui/button/index.js';
	import { Label } from '$lib/components/ui/label/index.js';
	import { Textarea } from '$lib/components/ui/textarea/index.js';
	import * as Card from '$lib/components/ui/card/index.js';
	import SaveButton from '$lib/components/custom/save-button/save-button.svelte';
	import DeleteConfirmDialog from '$lib/components/custom/delete-confirm-dialog/delete-confirm-dialog.svelte';
	import { toast } from 'svelte-sonner';
	import type { Snippet } from 'svelte';

	let {
		corsXml,
		loading = false,
		title = 'CORS Configuration',
		description = '',
		onsave,
		ondelete,
		skeleton,
	}: {
		corsXml: string;
		loading?: boolean;
		title?: string;
		description?: string;
		onsave: (xml: string) => Promise<void>;
		ondelete?: () => Promise<void>;
		skeleton?: Snippet;
	} = $props();

	let syncVersion = $state(0);
	let localCorsXml = $state('');

	$effect(() => {
		void corsXml;
		void syncVersion;
		localCorsXml = corsXml ?? '';
	});

	let dirty = $derived(localCorsXml !== (corsXml ?? ''));
	let saving = $state(false);
	let deleteOpen = $state(false);
	let deleting = $state(false);

	async function save() {
		saving = true;
		try {
			await onsave(localCorsXml);
			syncVersion++;
			toast.success('CORS configuration saved');
		} catch {
			toast.error('Failed to save CORS configuration');
		} finally {
			saving = false;
		}
	}

	async function handleDelete() {
		if (!ondelete) return;
		deleting = true;
		try {
			await ondelete();
			syncVersion++;
			deleteOpen = false;
			toast.success('CORS configuration deleted');
		} catch {
			toast.error('Failed to delete CORS configuration');
		} finally {
			deleting = false;
		}
	}
</script>

<Card.Root class="flex h-full flex-col">
	<Card.Header>
		<Card.Title>{title}</Card.Title>
		{#if description}
			<Card.Description>{description}</Card.Description>
		{/if}
	</Card.Header>
	{#if loading}
		{#if skeleton}
			{@render skeleton()}
		{:else}
			<Card.Content class="flex-1">
				<div class="h-32 animate-pulse rounded bg-muted"></div>
			</Card.Content>
		{/if}
	{:else}
		<Card.Content class="flex-1 space-y-4">
			<div class="space-y-2">
				<Label for="cors-xml">CORS Rules (XML)</Label>
				<Textarea
					id="cors-xml"
					class="min-h-[120px] font-mono"
					placeholder="<CORSConfiguration>&#10;  <CORSRule>&#10;    <AllowedOrigin>*</AllowedOrigin>&#10;    <AllowedMethod>GET</AllowedMethod>&#10;    <AllowedMethod>PUT</AllowedMethod>&#10;    <AllowedHeader>*</AllowedHeader>&#10;    <ExposeHeader>ETag</ExposeHeader>&#10;  </CORSRule>&#10;</CORSConfiguration>"
					bind:value={localCorsXml}
				/>
				<p class="text-xs text-muted-foreground">
					Define allowed origins, methods, and headers using XML CORSRule elements.
				</p>
				<details class="rounded-md border bg-muted/50 px-3 py-2">
					<summary class="cursor-pointer text-xs font-medium text-muted-foreground">
						Example: CORS for presigned uploads
					</summary>
					<pre class="mt-2 overflow-x-auto text-xs text-muted-foreground"><code
							>&lt;CORSConfiguration&gt;
  &lt;CORSRule&gt;
    &lt;AllowedOrigin&gt;https://your-frontend.com&lt;/AllowedOrigin&gt;
    &lt;AllowedMethod&gt;GET&lt;/AllowedMethod&gt;
    &lt;AllowedMethod&gt;PUT&lt;/AllowedMethod&gt;
    &lt;AllowedHeader&gt;*&lt;/AllowedHeader&gt;
    &lt;ExposeHeader&gt;ETag&lt;/ExposeHeader&gt;
  &lt;/CORSRule&gt;
&lt;/CORSConfiguration&gt;</code
						></pre>
					<p class="mt-1 text-xs text-muted-foreground">
						Presigned multipart uploads require <strong>PUT</strong> in AllowedMethods and
						<strong>ETag</strong> in ExposeHeaders. Without this, browser uploads directly to HCP will
						fail.
					</p>
				</details>
			</div>
		</Card.Content>
		<Card.Footer class="gap-3">
			<SaveButton {dirty} {saving} onclick={save} />
			{#if ondelete && corsXml}
				<Button variant="destructive" size="sm" onclick={() => (deleteOpen = true)}>
					Delete CORS
				</Button>
			{/if}
		</Card.Footer>
	{/if}
</Card.Root>

<DeleteConfirmDialog
	bind:open={deleteOpen}
	name="CORS configuration"
	itemType="CORS configuration"
	loading={deleting}
	onconfirm={handleDelete}
/>
