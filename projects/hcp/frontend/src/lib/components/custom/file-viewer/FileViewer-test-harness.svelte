<script lang="ts">
	import FileViewer from './FileViewer.svelte';
	import * as Tooltip from '$lib/components/ui/tooltip/index.js';

	let open = $state(true);
	let currentIndex = $state(0);

	const files = [
		{
			filename: 'photo-001.jpg',
			url: 'https://placehold.co/400x300/1a1a2e/eee?text=Photo+001',
			size: 245_760,
			objectKey: 'photos/photo-001.jpg',
			lastModified: '2024-11-15T10:30:00Z',
			etag: '"abc123"',
			storageClass: 'STANDARD',
		},
		{
			filename: 'photo-002.png',
			url: 'https://placehold.co/400x300/2d3436/dfe6e9?text=Photo+002',
			size: 512_000,
			objectKey: 'photos/photo-002.png',
			lastModified: '2024-11-16T14:00:00Z',
			etag: '"def456"',
			storageClass: 'STANDARD',
		},
		{
			filename: 'data-export.parquet',
			url: '#',
			size: 52_428_800,
			objectKey: 'exports/data-export.parquet',
			lastModified: '2024-12-01T08:00:00Z',
			storageClass: 'GLACIER',
		},
	];

	let current = $derived(files[currentIndex]);

	function prev() {
		if (currentIndex > 0) currentIndex--;
	}
	function next() {
		if (currentIndex < files.length - 1) currentIndex++;
	}
</script>

<Tooltip.Provider>
	<div class="p-4">
		{#if !open}
			<button data-testid="reopen-btn" onclick={() => (open = true)}>Reopen</button>
		{/if}
		<div data-testid="current-file" class="text-xs text-muted-foreground">
			{current.filename}
		</div>
		<FileViewer
			bind:open
			filename={current.filename}
			url={current.url}
			size={current.size}
			objectKey={current.objectKey}
			lastModified={current.lastModified}
			etag={current.etag}
			storageClass={current.storageClass}
			hasPrev={currentIndex > 0}
			hasNext={currentIndex < files.length - 1}
			onprev={prev}
			onnext={next}
			onclose={() => (open = false)}
			{currentIndex}
			totalCount={files.length}
		/>
	</div>
</Tooltip.Provider>
