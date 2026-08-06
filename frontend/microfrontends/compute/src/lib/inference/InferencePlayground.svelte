<script lang="ts">
	// `/playground` — run ONE page image through HTR and read what comes back.
	//
	// The bytes ride `POST {base}/api/infer` (a `+server.ts`), never a remote function: an upload up
	// and an ALTO document down are both binary, and the status code is load-bearing here — see that
	// route's header for the full reasoning and for why the gateway is not in this path.
	import { base } from '$app/paths';
	import { Badge } from '@rask/ui/badge';
	import { Button } from '@rask/ui/button';
	import { Card } from '@rask/ui/card';
	import { Separator } from '@rask/ui/separator';
	import { PlugZap, RefreshCw, ScanText, ShieldAlert, Upload } from '@lucide/svelte';

	/** What the BFF reports when the call did not produce ALTO. `reason` is the discriminator. */
	type Failure = {
		reason: string;
		status?: number;
		detail?: string;
		serveUrl?: string;
	};

	let file = $state<File | null>(null);
	let previewUrl = $state<string | null>(null);
	let busy = $state(false);
	let alto = $state<string | null>(null);
	let lines = $state<string[]>([]);
	let failure = $state<Failure | null>(null);
	let elapsedMs = $state<number | null>(null);

	// Object URLs are a manual allocation; release the previous one on every swap and the last one
	// when the component goes away, or a few uploads leak the images they previewed.
	$effect(() => () => {
		if (previewUrl) URL.revokeObjectURL(previewUrl);
	});

	function pick(event: Event): void {
		const input = event.currentTarget as HTMLInputElement;
		const next = input.files?.[0] ?? null;
		if (previewUrl) URL.revokeObjectURL(previewUrl);
		previewUrl = next ? URL.createObjectURL(next) : null;
		file = next;
		alto = null;
		lines = [];
		failure = null;
		elapsedMs = null;
	}

	/**
	 * The transcribed lines, read out of the ALTO the model returned.
	 *
	 * Browser-only (`DOMParser`), so it is called from a handler and never at component top level —
	 * touching it during SSR would crash the render.
	 *
	 * Reconstructed per `<TextLine>` (joining its `<String CONTENT>` tokens) rather than by flattening
	 * every String in the document, because the flat reading silently concatenates separate lines into
	 * one paragraph and would misrepresent the model's own segmentation. If a document carries no
	 * TextLine at all, the flat reading is the honest fallback rather than an empty answer.
	 */
	function textLines(xml: string): string[] {
		try {
			const doc = new DOMParser().parseFromString(xml, 'application/xml');
			if (doc.querySelector('parsererror')) return [];
			const byLine = [...doc.getElementsByTagName('TextLine')]
				.map((tl) =>
					[...tl.getElementsByTagName('String')]
						.map((s) => s.getAttribute('CONTENT') ?? '')
						.join(' ')
						.trim(),
				)
				.filter((t) => t !== '');
			if (byLine.length > 0) return byLine;
			return [...doc.getElementsByTagName('String')]
				.map((s) => (s.getAttribute('CONTENT') ?? '').trim())
				.filter((t) => t !== '');
		} catch {
			// A result we cannot parse is still a result — the raw XML below stays readable.
			return [];
		}
	}

	async function run(): Promise<void> {
		if (!file || busy) return;
		busy = true;
		alto = null;
		lines = [];
		failure = null;
		elapsedMs = null;
		const started = performance.now();
		try {
			const res = await fetch(`${base}/api/infer?name=${encodeURIComponent(file.name)}`, {
				method: 'POST',
				headers: { 'content-type': file.type || 'application/octet-stream' },
				body: file,
			});
			elapsedMs = Math.round(performance.now() - started);
			if (res.ok) {
				const xml = await res.text();
				alto = xml;
				lines = textLines(xml);
				return;
			}
			// The BFF answers a problem body with a `reason`; SvelteKit's own `error()` answers
			// `{ message }`. Both are JSON, so read once and normalise.
			const body: unknown = await res.json().catch(() => null);
			const parsed = (body ?? {}) as Partial<Failure> & { message?: string };
			failure = parsed.reason
				? (parsed as Failure)
				: {
						reason: res.status === 401 ? 'unauthorized' : 'request_failed',
						status: res.status,
						detail: parsed.message ?? `HTTP ${res.status}`,
					};
		} catch (err) {
			failure = { reason: 'network', detail: String(err) };
		} finally {
			busy = false;
		}
	}

	// Return here after the OIDC round-trip (the shell's ?redirect= contract, nav-user.svelte).
	const loginHref = $derived(`/auth/login?redirect=${encodeURIComponent(`${base}/playground`)}`);
</script>

<div class="mx-auto flex w-full max-w-5xl flex-col gap-5 p-6">
	<header class="flex items-center gap-3">
		<div
			class="bg-sidebar-primary text-sidebar-primary-foreground flex size-10 items-center justify-center rounded-xl"
		>
			<ScanText class="size-5" />
		</div>
		<div class="min-w-0 flex-1">
			<h1 class="text-xl font-semibold tracking-tight">Inference playground</h1>
			<p class="text-muted-foreground text-sm">
				Send one page image through HTR and read the ALTO it returns.
			</p>
		</div>
		<Badge variant="outline">Direct to Ray Serve</Badge>
	</header>

	<Card class="flex flex-col gap-4 p-5">
		<div class="flex flex-wrap items-center gap-3">
			<label
				class="border-input bg-background hover:bg-accent hover:text-accent-foreground inline-flex cursor-pointer items-center gap-2 rounded-md border px-3 py-2 text-sm font-medium"
			>
				<Upload class="size-4" />
				{file ? 'Choose another image' : 'Choose an image'}
				<input type="file" accept="image/*" class="hidden" onchange={pick} />
			</label>
			{#if file}
				<span class="text-muted-foreground truncate text-xs">
					{file.name} · {(file.size / 1024).toFixed(0)} kB
				</span>
			{/if}
			<div class="flex-1"></div>
			<Button disabled={!file || busy} onclick={run}>
				{#if busy}
					<RefreshCw class="size-4 animate-spin" /> Transcribing…
				{:else}
					<ScanText class="size-4" /> Run HTR
				{/if}
			</Button>
		</div>

		{#if previewUrl}
			<Separator />
			<img
				src={previewUrl}
				alt="The page about to be transcribed"
				class="border-border max-h-80 w-auto self-start rounded-md border object-contain"
			/>
		{/if}
	</Card>

	{#if failure}
		<Card class="border-destructive/40 flex flex-col gap-2 p-5">
			<div class="flex items-center gap-2">
				{#if failure.reason === 'unauthorized'}
					<ShieldAlert class="text-destructive size-4" />
					<h2 class="text-sm font-semibold">Sign in to run inference</h2>
				{:else}
					<PlugZap class="text-destructive size-4" />
					<h2 class="text-sm font-semibold">
						{#if failure.reason === 'upstream_unreachable'}
							No Ray Serve answered
						{:else if failure.reason === 'wrong_serve_app'}
							Serve is up, but this route has no HTTP handler
						{:else}
							Inference failed
						{/if}
					</h2>
				{/if}
			</div>

			{#if failure.reason === 'unauthorized'}
				<p class="text-muted-foreground text-sm">
					This stack is governed and inference spends real GPU time —
					<a href={loginHref} data-sveltekit-reload class="text-foreground underline underline-offset-2"
						>sign in</a
					> to run it.
				</p>
			{:else if failure.reason === 'upstream_unreachable'}
				<p class="text-muted-foreground text-sm">
					Nothing is listening at <code class="text-foreground">{failure.serveUrl}</code>. Start the
					HTRflow deployment with <code class="text-foreground">make serve-up-htrflow</code>, or point
					<code class="text-foreground">COMPUTE_SERVE_URL</code> at a live Serve ingress.
				</p>
			{:else if failure.reason === 'wrong_serve_app'}
				<p class="text-muted-foreground text-sm">
					<code class="text-foreground">{failure.serveUrl}</code> answered 405. That is what
					<code class="text-foreground">/transcribe</code> does — it is callable only over a Serve
					handle, not over HTTP. Plain <code class="text-foreground">make serve-up</code> deploys exactly
					that one; run <code class="text-foreground">make serve-up-htrflow</code> (or
					<code class="text-foreground">serve-up-both</code>) to get the HTTP-callable app.
				</p>
			{:else}
				<p class="text-muted-foreground text-sm">
					{failure.status ? `HTTP ${failure.status}` : 'The request did not complete'}.
				</p>
			{/if}

			{#if failure.detail}
				<pre
					class="bg-muted text-muted-foreground max-h-40 overflow-auto rounded-md p-3 text-xs whitespace-pre-wrap">{failure.detail}</pre>
			{/if}
		</Card>
	{/if}

	{#if alto !== null}
		<Card class="flex flex-col gap-3 p-5">
			<div class="flex items-center gap-2">
				<h2 class="flex-1 text-sm font-semibold">
					Transcription
					<span class="text-muted-foreground font-normal">
						· {lines.length}
						{lines.length === 1 ? 'line' : 'lines'}{elapsedMs !== null ? ` · ${elapsedMs} ms` : ''}
					</span>
				</h2>
				<Badge variant="success">ALTO returned</Badge>
			</div>

			{#if lines.length === 0}
				<p class="text-muted-foreground text-sm">
					The model returned a document with no readable text lines. The raw ALTO is below — that is the
					model's actual answer, not an error.
				</p>
			{:else}
				<div class="border-border flex flex-col gap-1 rounded-md border p-3">
					{#each lines as line, i (i)}
						<p class="text-sm leading-relaxed">{line}</p>
					{/each}
				</div>
			{/if}

			<Separator />
			<details>
				<summary class="text-muted-foreground cursor-pointer text-xs">Raw ALTO XML</summary>
				<pre
					class="bg-muted text-muted-foreground mt-2 max-h-96 overflow-auto rounded-md p-3 text-xs">{alto}</pre>
			</details>
		</Card>
	{/if}

	<p class="text-muted-foreground text-xs">
		This page does <strong>not</strong> go through the gateway. Inference has no route there today:
		<code>/api/serve/*</code> reaches Ray's dashboard API on :8265 and is registered
		<code>GET</code>/<code>HEAD</code> only, so a POST is answered 405 — deliberately, because the
		same surface can deploy arbitrary code. The bytes therefore go from this zone's server straight to
		the Serve ingress at <code>COMPUTE_SERVE_URL</code> (default
		<code>http://localhost:8000/htrflow</code>), which is how the medallion's own HTR stage reaches it
		too. Giving <code>/api/serve</code> an authenticated inference POST is a backend change, not a frontend
		one.
	</p>
</div>
