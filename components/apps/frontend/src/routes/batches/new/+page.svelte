<script lang="ts">
	import { uploadVolume, type BatchRow } from '@rask/api';
	import RayShell from '$lib/components/layout/ray-shell.svelte';
	import { Card } from '$lib/components/ui/card';
	import { Button } from '@rask/ui/button';
	import { Upload, X, FileImage } from 'lucide-svelte';

	const IMAGE_RE = /\.(jpe?g|png|tiff?)$/i;
	const ID_RE = /^[A-Za-z0-9_-]+$/;

	let volumeId = $state('');
	let files = $state<File[]>([]);
	let dragOver = $state(false);
	let busy = $state(false);
	let error = $state<string | null>(null);
	let result = $state<BatchRow | null>(null);

	const validId = $derived(ID_RE.test(volumeId));
	const canIngest = $derived(validId && files.length > 0 && !busy);

	function addFiles(list: FileList | null) {
		if (!list) return;
		const names = new Set(files.map((f) => f.name));
		const incoming = Array.from(list).filter((f) => IMAGE_RE.test(f.name) && !names.has(f.name));
		files = [...files, ...incoming];
	}
	function removeFile(name: string) {
		files = files.filter((f) => f.name !== name);
	}
	function onDrop(e: DragEvent) {
		e.preventDefault();
		dragOver = false;
		addFiles(e.dataTransfer?.files ?? null);
	}
	function fmtSize(n: number): string {
		if (n < 1024) return `${n} B`;
		if (n < 1024 * 1024) return `${(n / 1024).toFixed(0)} KB`;
		return `${(n / 1024 / 1024).toFixed(1)} MB`;
	}
	async function ingest() {
		busy = true;
		error = null;
		result = null;
		try {
			result = await uploadVolume(volumeId, files);
		} catch (e: unknown) {
			error = e instanceof Error ? e.message : String(e);
		} finally {
			busy = false;
		}
	}
</script>

<RayShell title="New volume">
	<Card class="m-4 max-w-2xl space-y-4 p-6">
		<h1 class="text-lg font-semibold">Upload images</h1>

		<label class="block space-y-1">
			<span class="text-sm font-medium">Volume name</span>
			<input
				class="bg-background w-full rounded border px-3 py-2"
				placeholder="e.g. my_volume"
				bind:value={volumeId}
				disabled={busy}
			/>
			{#if volumeId && !validId}
				<span class="text-destructive text-xs">Letters, digits, - and _ only.</span>
			{/if}
		</label>

		<div
			role="button"
			tabindex="0"
			class="rounded border-2 border-dashed p-8 text-center {dragOver ? 'border-primary bg-muted' : 'border-muted'}"
			ondragover={(e: DragEvent) => {
				e.preventDefault();
				dragOver = true;
			}}
			ondragleave={() => (dragOver = false)}
			ondrop={onDrop}
		>
			<Upload class="mx-auto mb-2 h-6 w-6 opacity-60" />
			<p class="text-sm">Drag &amp; drop images here, or</p>
			<label class="mt-2 inline-block">
				<input
					type="file"
					multiple
					accept="image/*"
					class="hidden"
					onchange={(e: Event & { currentTarget: HTMLInputElement }) =>
						addFiles(e.currentTarget.files)}
				/>
				<span class="cursor-pointer text-sm underline">browse</span>
			</label>
			<p class="mt-1 text-xs opacity-60">jpg, png, tif</p>
		</div>

		{#if files.length}
			<ul class="space-y-1 text-sm">
				{#each files as f (f.name)}
					<li class="flex items-center justify-between rounded border px-2 py-1">
						<span class="flex items-center gap-2 truncate"
							><FileImage class="h-4 w-4 opacity-60" />{f.name}</span
						>
						<span class="flex items-center gap-2 opacity-60">
							{fmtSize(f.size)}
							<button onclick={() => removeFile(f.name)} disabled={busy} aria-label="remove file">
								<X class="h-4 w-4" />
							</button>
						</span>
					</li>
				{/each}
			</ul>
		{/if}

		<Button onclick={ingest} disabled={!canIngest}>
			{busy ? 'Ingesting…' : `Ingest ${files.length || ''} image${files.length === 1 ? '' : 's'}`}
		</Button>

		{#if error}<p class="text-destructive text-sm">{error}</p>{/if}
		{#if result}
			<div class="space-y-1 rounded border border-green-600 p-3 text-sm">
				<p>Ingested <strong>{result.batch_id}</strong> — {result.page_count} pages.</p>
				<div class="flex gap-3">
					<a class="underline" href="/batches">Back to Batches</a>
					<a class="underline" href={`/viewer/${result.batch_id}`}>Open viewer</a>
				</div>
			</div>
		{/if}
	</Card>
</RayShell>
