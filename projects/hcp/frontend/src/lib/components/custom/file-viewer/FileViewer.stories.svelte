<script module lang="ts">
	import { defineMeta } from '@storybook/addon-svelte-csf';
	import { fn } from 'storybook/test';
	import FileViewer from './FileViewer.svelte';
	import * as Tooltip from '$lib/components/ui/tooltip/index.js';
	import type { ComponentProps } from 'svelte';

	type Args = ComponentProps<typeof FileViewer>;

	const { Story } = defineMeta({
		title: 'UI/FileViewer',
		component: FileViewer,
		render: template,
		args: {
			open: true,
			onclose: fn(),
		},
	});
</script>

{#snippet template(args: Args)}
	<Tooltip.Provider>
		<FileViewer {...args} />
	</Tooltip.Provider>
{/snippet}

<Story
	name="Image"
	args={{
		filename: 'architecture-diagram.png',
		url: 'https://placehold.co/800x600/1a1a2e/eee?text=Architecture+Diagram',
		size: 245_760,
		objectKey: 'docs/architecture-diagram.png',
		lastModified: '2024-11-15T10:30:00Z',
		etag: '"d41d8cd98f00b204e9800998ecf8427e"',
		storageClass: 'STANDARD',
	}}
/>

<Story
	name="Unsupported File"
	args={{
		filename: 'data-export.parquet',
		url: '#',
		size: 52_428_800,
		objectKey: 'exports/data-export.parquet',
		lastModified: '2024-12-01T08:00:00Z',
		storageClass: 'GLACIER',
	}}
/>

<Story
	name="With Navigation"
	args={{
		filename: 'photo-003.jpg',
		url: 'https://placehold.co/600x400/2d3436/dfe6e9?text=Photo+003',
		size: 1_048_576,
		objectKey: 'photos/photo-003.jpg',
		hasPrev: true,
		hasNext: true,
		onprev: fn(),
		onnext: fn(),
		currentIndex: 2,
		totalCount: 10,
	}}
/>

<Story
	name="No Metadata"
	args={{
		filename: 'readme.txt',
		url: 'data:text/plain,Hello%20from%20Storybook!%0AThis%20is%20a%20preview%20of%20a%20text%20file.',
		size: 42,
	}}
/>

<Story
	name="Large File Info"
	args={{
		filename: 'database-backup.sql',
		url: '#',
		size: 5_368_709_120,
		objectKey: 'backups/2024/database-backup.sql',
		lastModified: '2024-12-20T03:00:00Z',
		etag: '"abc123def456"',
		storageClass: 'DEEP_ARCHIVE',
	}}
/>
