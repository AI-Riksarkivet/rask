<script lang="ts">
	// The object browser's PREVIEW pane (R18): metadata from the volumes-api HEAD, then a
	// kind-appropriate body — images render inline through the object byte proxy, text/XML/JSON
	// decode to a capped text block, anything else states its type and offers the download. Missing
	// (404) and unreachable states are explicit; a dead backend can never hang a spinner here.
	import { Download, RefreshCw, X } from '@lucide/svelte';
	import { untrack } from 'svelte';
	import {
		downloadUrl,
		fetchObjectBytes,
		fmtModified,
		fmtSize,
		headObject,
		previewKind,
		type Bucket,
		type S3ObjectHead,
	} from './storage';

	let { bucket, key, onclose }: { bucket: Bucket; key: string; onclose: () => void } = $props();

	// Caps keep the pane honest instead of heavy: nothing over 512 kB is pulled for text preview,
	// and a decoded body is trimmed to its first 20k characters with the trim stated.
	const TEXT_MAX_BYTES = 512 * 1024;
	const TEXT_MAX_CHARS = 20_000;

	let head = $state<S3ObjectHead | null>(null);
	let lastStatus = $state(0);
	let settled = $state(false);
	let text = $state<string | null>(null);
	let textNote = $state<string | null>(null);
	let textError = $state<string | null>(null);

	const missing = $derived(head === null && settled && lastStatus === 404);
	const offline = $derived(head === null && settled && lastStatus !== 404);
	const kind = $derived(previewKind(key, head?.content_type ?? null));
	const name = $derived(key.split('/').filter(Boolean).at(-1) ?? key);

	async function load(): Promise<void> {
		// Latest-wins: the pane is reused across selections — drop a stale response.
		const wantBucket = bucket;
		const wantKey = key;
		head = null;
		settled = false;
		text = null;
		textNote = null;
		textError = null;
		const res = await headObject(wantBucket, wantKey);
		if (bucket !== wantBucket || key !== wantKey) return;
		settled = true;
		if (!res.ok) {
			lastStatus = res.status;
			return;
		}
		head = res.data;
		lastStatus = 200;
		if (previewKind(wantKey, res.data.content_type) !== 'text') return;
		if (res.data.size > TEXT_MAX_BYTES) {
			textNote = `too large for inline text preview (${fmtSize(res.data.size)}) — download to view`;
			return;
		}
		const body = await fetchObjectBytes(wantBucket, wantKey);
		if (bucket !== wantBucket || key !== wantKey) return;
		if (!body.ok) {
			textError = `text preview failed (HTTP ${body.status})`;
			return;
		}
		const decoded = new TextDecoder('utf-8', { fatal: false }).decode(body.data);
		if (decoded.length > TEXT_MAX_CHARS) {
			text = decoded.slice(0, TEXT_MAX_CHARS);
			textNote = `showing the first ${TEXT_MAX_CHARS.toLocaleString('en')} characters of ${fmtSize(res.data.size)}`;
		} else {
			text = decoded;
		}
	}

	$effect(() => {
		// Reset + reload whenever the selection changes; untracked so load()'s reads don't retrigger.
		void bucket;
		void key;
		untrack(() => load());
	});
</script>

<aside class="pane" aria-label="Object preview">
	<header>
		<h2 class="mono">{name}</h2>
		<button class="icon" aria-label="Close preview" onclick={onclose}><X size={14} /></button>
	</header>
	{#if !settled}
		<p class="mut">Loading…</p>
	{:else if missing}
		<p class="mut">Object not found — it may have been removed since this listing.</p>
	{:else if offline}
		<div class="empty">
			<RefreshCw size={14} />
			<p>Volumes service unreachable (HTTP {lastStatus}).</p>
			<button class="btn" onclick={load}>Retry</button>
		</div>
	{:else if head !== null}
		<dl class="rec">
			<dt>key</dt>
			<dd class="mono">{head.key}</dd>
			<dt>size</dt>
			<dd class="mono">{fmtSize(head.size)} ({head.size} B)</dd>
			<dt>content type</dt>
			<dd class="mono">{head.content_type ?? '—'}</dd>
			<dt>modified</dt>
			<dd class="mono">{fmtModified(head.last_modified)}</dd>
			<dt>etag</dt>
			<dd class="mono">{head.etag ?? '—'}</dd>
		</dl>
		{#if kind === 'image'}
			<img
				class="preview"
				src={downloadUrl(bucket, key)}
				alt={`Preview of ${key}`}
				loading="lazy"
			/>
		{:else if kind === 'text'}
			{#if textError}
				<p class="error">{textError}</p>
			{:else if text === null && textNote === null}
				<p class="mut">Loading text…</p>
			{:else}
				{#if textNote}<p class="note">{textNote}</p>{/if}
				{#if text !== null}<pre class="text">{text}</pre>{/if}
			{/if}
		{:else}
			<p class="mut">No inline preview for this type ({head.content_type ?? 'unknown'}).</p>
		{/if}
		<a class="btn download" href={downloadUrl(bucket, key)} download>
			<Download size={13} /> Download
		</a>
	{/if}
</aside>

<style>
	.pane {
		border: 1px solid var(--line);
		border-radius: var(--radius-sm);
		background: var(--panel);
		padding: 12px 14px;
		align-self: start;
		min-width: 0;
	}
	header {
		display: flex;
		align-items: center;
		gap: 8px;
		margin-bottom: 10px;
	}
	h2 {
		font-size: 13px;
		margin: 0;
		flex: 1;
		min-width: 0;
		overflow: hidden;
		text-overflow: ellipsis;
		white-space: nowrap;
	}
	.icon {
		background: none;
		border: none;
		color: var(--mut);
		cursor: pointer;
		padding: 2px;
		display: inline-flex;
	}
	.icon:hover {
		color: var(--ink);
	}
	.rec {
		display: grid;
		grid-template-columns: max-content 1fr;
		gap: 5px 12px;
		margin: 0 0 12px;
		font-size: 12px;
	}
	.rec dt {
		color: var(--faint);
	}
	.rec dd {
		margin: 0;
		color: var(--ink);
		word-break: break-all;
	}
	.preview {
		max-width: 100%;
		max-height: 320px;
		object-fit: contain;
		border: 1px solid var(--line);
		border-radius: var(--radius-sm);
		background: var(--panel-2);
		margin-bottom: 12px;
	}
	.text {
		max-height: 320px;
		overflow: auto;
		background: var(--panel-2);
		border: 1px solid var(--line);
		border-radius: var(--radius-sm);
		padding: 8px 10px;
		font-size: 11px;
		line-height: 1.5;
		white-space: pre-wrap;
		word-break: break-all;
		margin: 0 0 12px;
	}
	.note {
		color: var(--faint);
		font-size: 12px;
		margin: 0 0 8px;
	}
	.btn {
		background: var(--panel-2);
		border: 1px solid var(--line);
		border-radius: var(--radius-sm);
		color: var(--ink);
		font-size: 12px;
		padding: 3px 10px;
		cursor: pointer;
	}
	.download {
		display: inline-flex;
		align-items: center;
		gap: 5px;
		text-decoration: none;
	}
	.download:hover {
		border-color: var(--mut);
	}
	.mut {
		color: var(--mut);
		font-size: 12px;
	}
	.error {
		color: var(--fail);
		font-size: 12px;
	}
	.empty {
		display: flex;
		align-items: center;
		gap: 8px;
		color: var(--mut);
		padding: 12px 0;
		font-size: 12px;
	}
	.empty p {
		margin: 0;
	}
</style>
