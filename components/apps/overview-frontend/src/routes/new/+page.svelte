<script lang="ts">
	import { base } from '$app/paths';
	import { uploadVolume, type BatchRow } from '@rask/api';
	import { Card } from '@rask/ui/card';
	import { Button } from '@rask/ui/button';
	import { Upload, X, FileImage } from '@lucide/svelte';

	const IMAGE_RE = /\.(jpe?g|png|tiff?)$/i;
	const ID_RE = /^[A-Za-z0-9_-]+$/;

	// This app's base is /default/overview; in-app links use `base`. The cross-domain
	// viewer link stays project-prefixed — derive the project segment from the base
	// (there's no [project] route param in this carved app).
	const project = base.split('/')[1] ?? 'default';

	let volumeId = $state('');
	let files = $state<File[]>([]);
	let dragOver = $state(false);
	let busy = $state(false);
	let error = $state<string | null>(null);
	let result = $state<BatchRow | null>(null);
	let skipped = $state(0);

	const validId = $derived(ID_RE.test(volumeId));
	const canIngest = $derived(validId && files.length > 0 && !busy);

	function addFiles(list: FileList | null) {
		if (!list) return;
		const names = new Set(files.map((f) => f.name));
		const all = Array.from(list);
		const incoming = all.filter((f) => IMAGE_RE.test(f.name) && !names.has(f.name));
		skipped = all.filter((f) => !IMAGE_RE.test(f.name)).length;
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

<svelte:head>
	<title>Upload images — RASK</title>
</svelte:head>

<main class="bg-background flex-1 overflow-auto">
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
			class="rounded border-2 border-dashed p-8 text-center {dragOver
				? 'border-primary bg-muted'
				: 'border-muted'}"
			ondragover={(e: DragEvent) => {
				e.preventDefault();
				dragOver = true;
			}}
			ondragleave={(e: DragEvent) => {
				if (!(e.currentTarget as Node).contains(e.relatedTarget as Node)) dragOver = false;
			}}
			ondrop={onDrop}
			onkeydown={(e: KeyboardEvent) => {
				if (e.key === 'Enter' || e.key === ' ') {
					e.preventDefault();
					(e.currentTarget as HTMLElement)
						.querySelector<HTMLInputElement>('input[type=file]')
						?.click();
				}
			}}
		>
			<Upload class="mx-auto mb-2 h-6 w-6 opacity-60" />
			<p class="text-sm">Drag &amp; drop images here, or</p>
			<label class="mt-2 inline-block">
				<input
					type="file"
					multiple
					accept=".jpg,.jpeg,.png,.tif,.tiff,image/*"
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
		{#if skipped > 0}<p class="text-xs opacity-60">
				{skipped} non-image file{skipped === 1 ? '' : 's'} skipped.
			</p>{/if}

		<Button onclick={ingest} disabled={!canIngest}>
			{busy
				? 'Ingesting…'
				: files.length === 0
					? 'Ingest images'
					: `Ingest ${files.length} image${files.length === 1 ? '' : 's'}`}
		</Button>

		{#if error}<p class="text-destructive text-sm">{error}</p>{/if}
		{#if result}
			<div class="space-y-1 rounded border border-emerald-600 p-3 text-sm">
				<p>Ingested <strong>{result.batch_id}</strong> — {result.page_count ?? 0} pages.</p>
				<div class="flex gap-3">
					<a class="underline" href={base}>Back to Overview</a>
					<a class="underline" href={`/${project}/discover/viewer/${result.batch_id}`}
						>Open viewer</a
					>
				</div>
			</div>
		{/if}
	</Card>
</main>
