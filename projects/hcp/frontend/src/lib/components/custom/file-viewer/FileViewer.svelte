<script lang="ts">
	import * as Dialog from '$lib/components/ui/dialog/index.js';
	import * as Tooltip from '$lib/components/ui/tooltip/index.js';
	import { Button } from '$lib/components/ui/button/index.js';
	import { Badge } from '$lib/components/ui/badge/index.js';
	import {
		X,
		Download,
		FileText,
		Loader2,
		AlertCircle,
		ChevronLeft,
		ChevronRight,
		Info,
	} from 'lucide-svelte';
	import { formatBytes, formatDate } from '$lib/utils/format.js';

	interface Props {
		open: boolean;
		url: string;
		filename: string;
		size?: number;
		onclose: () => void;
		objectKey?: string;
		lastModified?: string;
		etag?: string;
		storageClass?: string;
		hasPrev?: boolean;
		hasNext?: boolean;
		onprev?: () => void;
		onnext?: () => void;
		currentIndex?: number;
		totalCount?: number;
	}

	let {
		open = $bindable(),
		url,
		filename,
		size,
		onclose,
		objectKey,
		lastModified,
		etag,
		storageClass,
		hasPrev = false,
		hasNext = false,
		onprev,
		onnext,
		currentIndex,
		totalCount,
	}: Props = $props();

	let textContent = $state<string | null>(null);
	let loading = $state(false);
	let error = $state<string | null>(null);
	let imageError = $state(false);
	let imageLoading = $state(true);
	let showMeta = $state(true);

	const ext = $derived(filename.split('.').pop()?.toLowerCase() ?? '');
	const category = $derived(getCategory(ext));

	function getCategory(e: string): 'image' | 'video' | 'audio' | 'pdf' | 'text' | 'unsupported' {
		if (['jpg', 'jpeg', 'png', 'gif', 'webp', 'svg', 'bmp', 'ico', 'avif'].includes(e))
			return 'image';
		if (['mp4', 'webm', 'ogg', 'mov'].includes(e)) return 'video';
		if (['mp3', 'wav', 'flac', 'aac', 'm4a'].includes(e)) return 'audio';
		if (e === 'pdf') return 'pdf';
		if (
			[
				'txt',
				'md',
				'markdown',
				'json',
				'csv',
				'tsv',
				'xml',
				'yaml',
				'yml',
				'toml',
				'ini',
				'cfg',
				'conf',
				'log',
				'env',
				'sh',
				'bash',
				'zsh',
				'fish',
				'js',
				'ts',
				'jsx',
				'tsx',
				'mjs',
				'cjs',
				'py',
				'rb',
				'rs',
				'go',
				'java',
				'kt',
				'c',
				'cpp',
				'h',
				'hpp',
				'cs',
				'swift',
				'r',
				'lua',
				'pl',
				'php',
				'sql',
				'html',
				'css',
				'scss',
				'less',
				'svelte',
				'vue',
				'dockerfile',
				'makefile',
				'gitignore',
				'editorconfig',
			].includes(e)
		)
			return 'text';
		return 'unsupported';
	}

	async function fetchText(fetchUrl: string) {
		loading = true;
		error = null;
		textContent = null;
		try {
			const res = await fetch(fetchUrl, { credentials: 'same-origin' });
			if (!res.ok) {
				throw new Error(`Failed to load file (HTTP ${res.status})`);
			}
			const text = await res.text();
			textContent =
				text.length > 512_000 ? text.slice(0, 512_000) + '\n\n... (truncated at 500KB)' : text;
		} catch (e) {
			error = e instanceof Error ? e.message : 'Failed to load file';
		} finally {
			loading = false;
		}
	}

	// Reset state and fetch text when url changes
	$effect(() => {
		const currentUrl = url;
		textContent = null;
		error = null;
		imageError = false;
		imageLoading = true;
		let cancelled = false;

		if (open && category === 'text' && currentUrl) {
			(async () => {
				loading = true;
				try {
					const res = await fetch(currentUrl, { credentials: 'same-origin' });
					if (cancelled) return;
					if (!res.ok) throw new Error(`Failed to load file (HTTP ${res.status})`);
					const text = await res.text();
					if (cancelled) return;
					textContent =
						text.length > 512_000 ? text.slice(0, 512_000) + '\n\n... (truncated at 500KB)' : text;
				} catch (e) {
					if (cancelled) return;
					error = e instanceof Error ? e.message : 'Failed to load file';
				} finally {
					if (!cancelled) loading = false;
				}
			})();
		}

		return () => {
			cancelled = true;
		};
	});

	function handleKeydown(e: KeyboardEvent) {
		if (!open) return;
		if (e.key === 'ArrowLeft' && hasPrev && onprev) {
			e.preventDefault();
			onprev();
		} else if (e.key === 'ArrowRight' && hasNext && onnext) {
			e.preventDefault();
			onnext();
		}
	}
</script>

<svelte:window onkeydown={handleKeydown} />

<Dialog.Root
	bind:open
	onOpenChange={(o) => {
		if (!o) onclose();
	}}
>
	<Dialog.Content showCloseButton={false} class="sm:max-w-6xl h-[90vh] flex flex-col p-0 gap-0">
		<!-- Header -->
		<div class="flex shrink-0 items-center justify-between border-b px-4 py-3">
			<div class="flex min-w-0 items-center gap-3">
				<FileText class="h-5 w-5 shrink-0 text-muted-foreground" />
				<div class="min-w-0">
					<div class="flex items-center gap-2">
						<Dialog.Title class="truncate text-sm font-semibold">
							{filename}
						</Dialog.Title>
						{#if currentIndex != null && totalCount != null}
							<span class="shrink-0 text-xs text-muted-foreground">
								{currentIndex + 1} of {totalCount}
							</span>
						{/if}
					</div>
					<Dialog.Description class="text-xs text-muted-foreground">
						{#if size != null}{formatBytes(size)} &middot;
						{/if}.{ext} file
					</Dialog.Description>
				</div>
			</div>
			<div class="flex items-center gap-1">
				{#if objectKey}
					<Tooltip.Root>
						<Tooltip.Trigger>
							{#snippet child({ props })}
								<Button
									{...props}
									variant="ghost"
									size="icon"
									onclick={() => (showMeta = !showMeta)}
								>
									<Info class="h-4 w-4" />
									<span class="sr-only">Toggle metadata</span>
								</Button>
							{/snippet}
						</Tooltip.Trigger>
						<Tooltip.Content>{showMeta ? 'Hide' : 'Show'} metadata</Tooltip.Content>
					</Tooltip.Root>
				{/if}
				<Button variant="ghost" size="icon" href={url} download={filename}>
					<Download class="h-4 w-4" />
					<span class="sr-only">Download</span>
				</Button>
				<Button variant="ghost" size="icon" onclick={onclose}>
					<X class="h-5 w-5" />
					<span class="sr-only">Close</span>
				</Button>
			</div>
		</div>

		<!-- Body: content + optional right sidebar -->
		<div class="flex min-h-0 flex-1">
			<!-- Content with navigation arrows -->
			<div class="relative flex min-w-0 flex-1">
				<!-- Left arrow -->
				{#if hasPrev && onprev}
					<Button
						variant="secondary"
						size="icon"
						class="absolute left-2 top-1/2 z-10 h-10 w-10 -translate-y-1/2 rounded-full bg-background/80 shadow-md backdrop-blur-sm hover:bg-background"
						onclick={onprev}
					>
						<ChevronLeft class="h-5 w-5" />
						<span class="sr-only">Previous file</span>
					</Button>
				{/if}

				<!-- Right arrow -->
				{#if hasNext && onnext}
					<Button
						variant="secondary"
						size="icon"
						class="absolute right-2 top-1/2 z-10 h-10 w-10 -translate-y-1/2 rounded-full bg-background/80 shadow-md backdrop-blur-sm hover:bg-background"
						onclick={onnext}
					>
						<ChevronRight class="h-5 w-5" />
						<span class="sr-only">Next file</span>
					</Button>
				{/if}

				{#if category === 'image'}
					<div class="flex flex-1 items-center justify-center overflow-auto bg-muted/50 p-4">
						{#if imageError}
							<div class="flex flex-col items-center gap-3">
								<AlertCircle class="h-8 w-8 text-destructive" />
								<p class="text-sm text-destructive">Failed to load image</p>
								<Button
									variant="secondary"
									size="sm"
									onclick={() => {
										imageError = false;
										imageLoading = true;
									}}>Retry</Button
								>
							</div>
						{:else}
							{#if imageLoading}
								<div class="absolute inset-0 flex items-center justify-center">
									<Loader2 class="h-5 w-5 animate-spin text-muted-foreground" />
								</div>
							{/if}
							<img
								src={url}
								alt={filename}
								class="max-h-full max-w-full rounded object-contain"
								onload={() => (imageLoading = false)}
								onerror={() => {
									imageLoading = false;
									imageError = true;
								}}
							/>
						{/if}
					</div>
				{:else if category === 'video'}
					<div class="flex flex-1 items-center justify-center overflow-auto bg-muted/50 p-4">
						<video controls class="max-h-full max-w-full rounded" src={url}>
							<track kind="captions" />
							Your browser does not support this video.
						</video>
					</div>
				{:else if category === 'audio'}
					<div class="flex flex-1 items-center justify-center overflow-auto bg-muted/50 p-4">
						<div class="flex flex-col items-center gap-4">
							<div class="flex h-24 w-24 items-center justify-center rounded-full bg-primary/10">
								<FileText class="h-10 w-10 text-primary" />
							</div>
							<audio controls src={url}> Your browser does not support this audio. </audio>
						</div>
					</div>
				{:else if category === 'pdf'}
					<div class="flex-1 overflow-hidden bg-muted/50">
						<iframe src={url} class="h-full w-full border-0" title={filename}></iframe>
					</div>
				{:else if category === 'text'}
					<div class="flex flex-1 flex-col overflow-hidden">
						{#if loading}
							<div class="flex flex-1 items-center justify-center gap-2 text-muted-foreground">
								<Loader2 class="h-5 w-5 animate-spin" />
								<span>Loading file content...</span>
							</div>
						{:else if error}
							<div class="flex flex-1 flex-col items-center justify-center gap-3">
								<AlertCircle class="h-8 w-8 text-destructive" />
								<p class="text-sm text-destructive">{error}</p>
								<Button variant="secondary" size="sm" onclick={() => fetchText(url)}>Retry</Button>
							</div>
						{:else if textContent != null}
							<div class="flex-1 overflow-auto p-4">
								<pre
									class="whitespace-pre-wrap break-words font-mono text-sm leading-relaxed text-foreground">{textContent}</pre>
							</div>
						{:else}
							<div class="flex flex-1 items-center justify-center text-muted-foreground">
								<span>No content to display</span>
							</div>
						{/if}
					</div>
				{:else}
					<!-- Unsupported -->
					<div
						class="flex flex-1 flex-col items-center justify-center gap-3 bg-muted/50 p-8 text-center"
					>
						<div class="flex h-16 w-16 items-center justify-center rounded-full bg-muted">
							<FileText class="h-8 w-8 text-muted-foreground" />
						</div>
						<p class="text-sm font-medium">
							Preview not available for .{ext} files
						</p>
						<p class="text-xs text-muted-foreground">Download the file to view it locally</p>
						<Button href={url} download={filename} class="mt-2">
							<Download class="h-4 w-4" />
							Download
						</Button>
						<p class="mt-4 text-xs text-muted-foreground">
							Parquet, DuckDB, and LanceDB viewers coming soon
						</p>
					</div>
				{/if}
			</div>

			<!-- Metadata Side Panel (right) -->
			{#if showMeta && objectKey}
				<div class="w-64 shrink-0 overflow-y-auto border-l bg-muted/30 p-4">
					<h3 class="mb-3 text-xs font-semibold uppercase tracking-wide text-muted-foreground">
						Details
					</h3>
					<div class="space-y-3">
						<div>
							<span class="text-xs font-medium text-muted-foreground">Key</span>
							<Tooltip.Root>
								<Tooltip.Trigger>
									{#snippet child({ props })}
										<p class="mt-0.5 truncate font-mono text-xs" {...props}>
											{objectKey}
										</p>
									{/snippet}
								</Tooltip.Trigger>
								<Tooltip.Content class="max-w-lg break-all font-mono text-xs"
									>{objectKey}</Tooltip.Content
								>
							</Tooltip.Root>
						</div>
						{#if size != null}
							<div>
								<span class="text-xs font-medium text-muted-foreground">Size</span>
								<p class="mt-0.5 text-xs">{formatBytes(size)}</p>
							</div>
						{/if}
						{#if lastModified}
							<div>
								<span class="text-xs font-medium text-muted-foreground">Last Modified</span>
								<p class="mt-0.5 text-xs">{formatDate(lastModified)}</p>
							</div>
						{/if}
						{#if etag}
							<div>
								<span class="text-xs font-medium text-muted-foreground">ETag</span>
								<p class="mt-0.5 truncate font-mono text-xs">{etag}</p>
							</div>
						{/if}
						{#if storageClass}
							<div>
								<span class="text-xs font-medium text-muted-foreground">Storage Class</span>
								<p class="mt-0.5"><Badge variant="secondary">{storageClass}</Badge></p>
							</div>
						{/if}
					</div>
				</div>
			{/if}
		</div>
	</Dialog.Content>
</Dialog.Root>
