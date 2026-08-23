<script lang="ts">
	// The right-hand drawer: the verdict, the selected node's raw tuples, and the model — the three
	// things that were separate tabs. They live here BESIDE the canvas, not instead of it, so reading a
	// tuple never costs you the graph you were reading it about.
	import { AlertDialog } from '@rask/ui/alert-dialog';
	import { Badge } from '@rask/ui/badge';
	import { Button } from '@rask/ui/button';
	import { Dialog } from '@rask/ui/dialog';
	import { Input } from '@rask/ui/input';
	import { Skeleton } from '@rask/ui/skeleton';
	import {
		Check,
		CircleCheck,
		CircleX,
		ClipboardCopy,
		Plus,
		ShieldAlert,
		Trash2,
		TriangleAlert,
	} from '@lucide/svelte';
	import { assertionName, toFgaYaml } from './assertion';
	import type { Tuple } from '../access';
	import {
		deleteTuple,
		fetchTuples,
		listUsers,
		simulate,
		writeTuple,
	} from '../remote/access.remote';

	type Props = {
		selected: string | null;
		verdict: { allowed: boolean; user: string; relation: string; object: string } | null;
		/** The derivation, already flattened to words by the explorer — rendered here as the chain. */
		chain: readonly string[];
		dsl: string | null;
		/** Relations the MODEL says are directly assignable on the selected object's type. A grant can
		 *  only ever name one of these — a derived `can_*` is rejected by the catalog — so the dialog
		 *  offers exactly them instead of a free-text box that learns the rule from a 400. */
		assignableRelations: readonly string[];
		/** The signed-in subject, so "grant to me" is one click and nobody has to know their own
		 *  opaque OIDC sub. */
		mySub: string | null;
		/** The stored tuples the current verdict rests on — exported WITH the assertion so the pasted
		 *  test is self-contained. See `toFgaYaml`: `fga model test` never reads the live store. */
		supportingTuples: readonly Tuple[];
		/** `{condition: {parameter: type}}` from the compiled model. Drives the grant dialog's TYPED
		 *  parameter inputs — a hand-kept copy would drift the moment the model gains a parameter, and a
		 *  missing CEL operand is a 503, not a soft failure. */
		modelConditions: Record<string, Record<string, string>>;
		onchanged: () => void;
	};

	let {
		selected,
		verdict,
		chain,
		dsl,
		assignableRelations,
		mySub,
		supportingTuples,
		modelConditions,
		onchanged,
	}: Props = $props();

	let tuples = $state<Tuple[] | null>(null);
	let status = $state(0);
	let message = $state<{ ok: boolean; text: string } | null>(null);
	let busy = $state(false);
	let showModel = $state(false);
	let inflight = 0;

	async function loadTuples(): Promise<void> {
		if (!selected) {
			tuples = null;
			return;
		}
		const seq = ++inflight;
		const res = await fetchTuples({ object: selected, pageSize: 100 });
		if (seq !== inflight) return; // latest-wins: a fast click-through must not resurrect a stale node
		if (!res.ok) {
			tuples = null;
			status = res.status;
			return;
		}
		tuples = res.data.tuples;
		status = 200;
	}

	$effect(() => {
		void selected;
		tuples = null;
		status = 0;
		loadTuples();
	});

	// ── revoke, with the blast radius measured BEFORE the write ──
	// "Are you sure?" is not an answer to "how many". A tuple high in the hierarchy can cut access for
	// every principal below it, and the only honest confirm names that number first.
	/**
	 * The verdict, rendered as a committable `.fga.yaml` test.
	 *
	 * Distinguishing a check RUN (a fact about this cluster now) from one COMMITTED (a fact CI enforces)
	 * is the whole point — they are not the same claim, and only the second survives the next deploy.
	 */
	const assertionYaml = $derived.by(() => {
		if (!verdict) return '';
		const a = {
			user: verdict.user,
			object: verdict.object,
			relation: verdict.relation,
			allowed: verdict.allowed,
		};
		return toFgaYaml(assertionName(a), [a], supportingTuples);
	});
	let copied = $state(false);

	async function copyAssertion(): Promise<void> {
		if (!assertionYaml) return;
		try {
			await navigator.clipboard.writeText(assertionYaml);
			copied = true;
			setTimeout(() => (copied = false), 2000);
		} catch (err) {
			// A denied clipboard permission is normal, not exceptional — the <pre> below is always
			// present and selectable, so the export never depends on the API being available.
			console.error(`clipboard write failed: ${String(err)}`);
		}
	}

	let revokeTarget = $state<Tuple | null>(null);
	let blast = $state<{ count: number; truncated: boolean } | null>(null);
	let blastPending = $state(false);

	async function askRevoke(t: Tuple): Promise<void> {
		revokeTarget = t;
		blast = null;
		blastPending = true;
		const res = await listUsers({ object: t.object, relation: t.relation });
		blastPending = false;
		if (revokeTarget !== t) return;
		if (!res.ok) return; // the dialog says "could not measure" — never a reassuring zero
		blast = { count: res.data.users.length, truncated: res.data.truncated };
	}

	async function revoke(): Promise<void> {
		const t = revokeTarget;
		if (!t || busy) return;
		busy = true;
		try {
			const res = await deleteTuple(t);
			message = res.ok
				? { ok: true, text: `Revoked (${t.user}, ${t.relation}, ${t.object}).` }
				: { ok: false, text: res.status === 403 ? 'Denied: estate-admin only.' : res.detail };
			if (res.ok) {
				await loadTuples();
				onchanged();
			}
		} finally {
			busy = false;
			revokeTarget = null;
			blast = null;
		}
	}

	// ── grant ──
	let grantOpen = $state(false);
	let gUser = $state('');
	let gRelation = $state('');
	/** The condition to attach, '' for a permanent grant. */
	let gCondition = $state('');
	/** Parameter values, keyed by name. Only the tuple-side parameters are collected — `current_time`
	 *  is the CALLER's clock at check time and must never be frozen into the grant. */
	let gParams = $state<Record<string, string>>({});

	/** Conditions this (type, relation) actually accepts, per the model. Empty ⇒ permanent-only, and the
	 *  control is not offered at all rather than offered and then rejected by the catalog. */
	const conditionChoices = $derived(Object.keys(modelConditions));

	/** The parameters the chosen condition needs FROM THE GRANT. `current_time` is excluded on purpose:
	 *  it is supplied per check, so collecting it here would pin "now" at the moment of granting and the
	 *  window would be evaluated against a frozen clock forever. */
	const conditionParams = $derived(
		Object.entries(modelConditions[gCondition] ?? {}).filter(([name]) => name !== 'current_time'),
	);

	/** A `duration` parameter is the one an operator actually thinks in, so it gets presets rather than
	 *  a free-text box that has to be taught Go duration syntax by a 400. */
	const DURATION_PRESETS = ['1h', '4h', '8h', '24h', '72h', '168h'];

	const conditionReady = $derived(
		!gCondition || conditionParams.every(([name]) => (gParams[name] ?? '').trim().length > 0),
	);

	/**
	 * What the proposed grant would actually change, answered by the store BEFORE anything is written.
	 *
	 * `allowed` on its own is unusable here: a grant that unlocks access and a grant that changes
	 * nothing both come back true. Only the delta against `baseline` separates them, and "this grant is
	 * a no-op" turns out to be the more common finding — a concentric model means the subject often
	 * already holds the access by inheritance, and granting again just adds a tuple somebody must later
	 * reason about when revoking.
	 */
	let preview = $state<{ allowed: boolean; baseline: boolean } | null>(null);
	let previewing = $state(false);
	let previewFailed = $state(false);

	async function runPreview(): Promise<void> {
		const subject = gUser.trim();
		const rel = gRelation.trim();
		if (!subject || !rel || !selected) return;
		previewing = true;
		preview = null;
		previewFailed = false;
		try {
			// The question is deliberately about the RELATION BEING GRANTED, on the object in hand —
			// "would writing this tuple make this true" — rather than some derived `can_*` the dialog
			// would have to guess at.
			const res = await simulate({
				user: subject,
				relation: rel,
				object: selected,
				hypothetical: [{ user: subject, relation: rel, object: selected }],
			});
			if (!res.ok) {
				previewFailed = true;
				return;
			}
			preview = { allowed: res.data.allowed, baseline: res.data.baseline };
		} catch (err) {
			// A rejected remote call is a transport failure, not a verdict — the dialog says so.
			console.error(`simulate transport failure: ${String(err)}`);
			previewFailed = true;
		} finally {
			previewing = false;
		}
	}

	async function grant(): Promise<void> {
		const t = {
			user: gUser.trim(),
			relation: gRelation.trim(),
			object: selected ?? '',
			// Only the tuple-side parameters. `grant_time` defaults to NOW at the moment of writing —
			// that is what "starting now, for 4 hours" means, and asking an operator to type an ISO
			// timestamp for the present instant is friction with no decision in it.
			...(gCondition
				? {
						condition: {
							name: gCondition,
							context: Object.fromEntries(
								conditionParams.map(([name, type]) => [
									name,
									(gParams[name] ?? '').trim() ||
										(type === 'timestamp' ? new Date().toISOString() : ''),
								]),
							),
						},
					}
				: {}),
		};
		if (busy || !t.user || !t.relation || !t.object) return;
		busy = true;
		try {
			const res = await writeTuple(t);
			message = res.ok
				? { ok: true, text: `Wrote (${t.user}, ${t.relation}, ${t.object}).` }
				: { ok: false, text: res.status === 403 ? 'Denied: estate-admin only.' : res.detail };
			if (res.ok) {
				grantOpen = false;
				gUser = gRelation = gCondition = '';
				gParams = {};
				preview = null;
				await loadTuples();
				onchanged();
			}
		} finally {
			busy = false;
		}
	}
</script>

<aside class="flex w-96 shrink-0 flex-col gap-4 overflow-y-auto border-l border-border pl-4">
	{#if verdict}
		<section
			class="flex flex-col gap-2 rounded-md border p-3 {verdict.allowed
				? 'border-success/50 bg-success/10'
				: 'border-destructive/50 bg-destructive/10'}"
		>
			<div class="flex items-center gap-2">
				{#if verdict.allowed}
					<CircleCheck size={20} class="text-success" />
					<span class="text-lg font-bold tracking-wide text-success">ALLOWED</span>
				{:else}
					<CircleX size={20} class="text-destructive" />
					<span class="text-lg font-bold tracking-wide text-destructive">DENIED</span>
				{/if}
			</div>
			<p class="font-mono text-[11px] text-muted-foreground">
				({verdict.user}, {verdict.relation}, {verdict.object})
			</p>
			{#if chain.length === 0}
				<p class="text-[11px] text-muted-foreground">
					{verdict.allowed
						? 'Allowed, but the store returned no derivation — the grant is direct.'
						: 'Denied, and nothing in the model derives it.'}
				</p>
			{/if}
		</section>
	{/if}

	{#if verdict}
		<section class="flex flex-col gap-1.5">
			<div class="flex items-center justify-between gap-2">
				<h3 class="text-[10px] font-semibold uppercase tracking-wider text-muted-foreground">
					Commit as a test
				</h3>
				<Button size="sm" variant="outline" class="h-6 text-[11px]" onclick={copyAssertion}>
					{#if copied}<Check size={11} /> Copied{:else}<ClipboardCopy size={11} /> Copy{/if}
				</Button>
			</div>
			<p class="text-[11px] text-muted-foreground">
				This verdict is true <em>now</em>. Paste into
				<span class="font-mono">model.fga.yaml</span> and CI enforces it on every change.
			</p>
			<pre
				class="overflow-x-auto rounded border border-border bg-muted p-2 font-mono text-[10px] leading-relaxed">{assertionYaml}</pre>
		</section>
	{/if}

	<!-- The result summary is OUTSIDE the verdict block on purpose. Only "why" produces a verdict;
	     "what can" and "who can" produce an answer with no boolean attached, and rendering their
	     summary inside `{#if verdict}` meant those two queries silently displayed nothing at all
	     beside the canvas. Found by the e2e spec, which is the only place the three shapes are
	     exercised against one component. -->
	{#if chain.length}
		<section class="flex flex-col gap-1">
			<h3 class="text-[10px] font-semibold uppercase tracking-wider text-muted-foreground">
				{verdict ? 'Derivation' : 'Result'}
			</h3>
			<!-- Hop by hop. A permission that resolves through three objects looks identical to a direct
			     grant in a tuple table, and they are not the same thing to revoke. -->
			<ol class="flex flex-col gap-0.5">
				{#each chain as step, i (step)}
					<li class="flex gap-1.5 font-mono text-[11px]">
						{#if verdict}<span class="tabular-nums text-muted-foreground">{i + 1}.</span>{/if}
						<span class={step.includes('EXCLUDED') ? 'text-destructive' : ''}>{step}</span>
					</li>
				{/each}
			</ol>
		</section>
	{/if}

	<section class="flex flex-col gap-2">
		<div class="flex items-center justify-between gap-2">
			<h2 class="text-xs font-semibold uppercase tracking-wider">Tuples</h2>
			{#if selected}
				<Button size="sm" variant="outline" onclick={() => (grantOpen = true)}>
					<Plus size={12} /> Grant
				</Button>
			{/if}
		</div>

		{#if !selected}
			<p class="text-xs text-muted-foreground">
				Select a node on the canvas to see the tuples stored on it.
			</p>
		{:else}
			<p class="truncate font-mono text-[11px] text-muted-foreground" title={selected}>
				{selected}
			</p>
			{#if status === 0 && tuples === null}
				<Skeleton class="h-16 w-full" />
			{:else if status === 401 || status === 403}
				<p class="flex items-center gap-1.5 text-xs text-muted-foreground">
					<ShieldAlert size={13} /> The tuple store is estate-admin only.
				</p>
			{:else if tuples === null}
				<p class="text-xs text-muted-foreground">Tuple store unreachable (HTTP {status}).</p>
			{:else if tuples.length === 0}
				<p class="text-xs text-muted-foreground">
					No tuples stored on this object — any access it has is derived.
				</p>
			{:else}
				<ul class="flex flex-col gap-1">
					{#each tuples as t (`${t.user}|${t.relation}|${t.object}`)}
						<li class="flex items-center gap-2 rounded border border-border px-2 py-1">
							<div class="min-w-0 flex-1">
								<div class="truncate font-mono text-[11px]">{t.user}</div>
								<Badge variant="secondary" class="mt-0.5 font-mono text-[9px]">{t.relation}</Badge>
							</div>
							<Button
								size="sm"
								variant="ghost"
								disabled={busy}
								aria-label="Revoke {t.user} {t.relation} {t.object}"
								onclick={() => askRevoke(t)}
							>
								<Trash2 size={12} />
							</Button>
						</li>
					{/each}
				</ul>
			{/if}
		{/if}

		{#if message}
			<p class="text-xs {message.ok ? 'text-success' : 'text-destructive'}">{message.text}</p>
		{/if}
	</section>

	{#if dsl}
		<section class="flex flex-col gap-2">
			<!-- "Model DSL", not "Model": the shared sidebar already owns a nav group called "Models", and
			     a bare "Model" collides with it for anything selecting by accessible name. -->
			<button
				class="flex items-center justify-between text-xs font-semibold uppercase tracking-wider text-muted-foreground hover:text-foreground"
				aria-expanded={showModel}
				onclick={() => (showModel = !showModel)}
			>
				Model DSL {showModel ? '−' : '+'}
			</button>
			{#if showModel}
				<!-- Read-only, permanently. The model lives in three checked-in files with a CI drift gate;
				     a UI that mutated it would make that gate a liar. Tuples are data; the model is code. -->
				<pre
					class="max-h-72 overflow-auto rounded border border-border bg-muted p-2 font-mono text-[10px] leading-relaxed">{dsl}</pre>
			{/if}
		</section>
	{/if}
</aside>

<Dialog.Root bind:open={grantOpen}>
	<Dialog.Content>
		<Dialog.Title>Grant on {selected}</Dialog.Title>
		<Dialog.Description>
			Writes one relationship tuple to the live store. Only a directly-assignable relation is
			accepted — a derived <span class="font-mono">can_*</span> is rejected by the catalog.
		</Dialog.Description>
		<form
			class="flex flex-col gap-2"
			onsubmit={(e) => {
				e.preventDefault();
				grant();
			}}
		>
			<div class="flex flex-col gap-1">
				<div class="flex items-center justify-between gap-2">
					<span class="text-xs font-medium">Subject</span>
					{#if mySub}
						<Button
							size="sm"
							variant="ghost"
							type="button"
							class="h-6 text-[11px]"
							onclick={() => {
								gUser = `user:${mySub}`;
								preview = null;
							}}
						>
							use my identity
						</Button>
					{/if}
				</div>
				<Input
					bind:value={gUser}
					class="font-mono text-xs"
					placeholder="user:&lt;oidc-sub&gt;"
					aria-label="Grant subject"
					oninput={() => {
						preview = null;
						previewFailed = false;
					}}
				/>
				<!-- The single most confusing thing about this store, stated rather than implied: a subject
				     is TYPE-PREFIXED, and for a person it is the OIDC `sub`, not a username. Under Dex that
				     is an opaque base64 blob, so `user:alice` is a valid-LOOKING id that matches nothing and
				     denies — a correct answer that reads exactly like a bug. -->
				<p class="text-[11px] text-muted-foreground">
					<span class="font-mono">user:&lt;oidc-sub&gt;</span> — not a username. Or a userset:
					<span class="font-mono">team:eng#member</span>,
					<span class="font-mono">role:validators#assignee</span>.
				</p>
			</div>

			<div class="flex flex-col gap-1">
				<span class="text-xs font-medium">Relation</span>
				{#if assignableRelations.length}
					<!-- Driven by the MODEL, so an ungrantable relation is unrepresentable rather than merely
					     discouraged. A derived `can_*` is computed and the catalog refuses to write it; a
					     free-text box taught that rule via a 400 one round trip later. -->
					<select
						bind:value={gRelation}
						class="rounded-md border border-border bg-background px-2 py-1.5 font-mono text-xs"
						aria-label="Grant relation"
						onchange={() => {
							preview = null;
							previewFailed = false;
						}}
					>
						<option value="" disabled>choose a relation…</option>
						{#each assignableRelations as r (r)}
							<option value={r}>{r}</option>
						{/each}
					</select>
					<p class="text-[11px] text-muted-foreground">
						Only directly-assignable relations on
						<span class="font-mono">{selected?.split(':')[0]}</span>. A derived
						<span class="font-mono">can_*</span> is computed by the model and cannot be granted.
					</p>

					<!-- ── time-boxed grants ──────────────────────────────────────────────────────────────────
			     A grant that expires on its own. The failure this kills is the contractor who still has
			     `writer` on bronze eight months after the engagement ended, because revoking was a
			     separate task nobody owned. The condition list and its parameters come from the MODEL
			     (GET /v1/access/model), so an unsupported condition or a missing operand is
			     unrepresentable here rather than a 400 one round trip later. -->
					{#if conditionChoices.length}
						<div class="flex flex-col gap-1 rounded-md border border-border p-2">
							<label class="flex items-center gap-2 text-xs font-medium">
								<input
									type="checkbox"
									checked={gCondition !== ''}
									aria-label="Time-box this grant"
									onchange={(e) => {
										gCondition = e.currentTarget.checked ? (conditionChoices[0] ?? '') : '';
										gParams = {};
										preview = null;
									}}
								/>
								Time-box this grant
							</label>

							{#if gCondition}
								<p class="text-[11px] text-muted-foreground">
									Expires by itself — nobody has to remember to revoke it. Evaluated by
									<span class="font-mono">{gCondition}</span> at every check.
								</p>
								{#each conditionParams as [name, type] (name)}
									<div class="flex flex-col gap-1 pt-1">
										<span class="text-[11px] font-medium">
											{name}
											<span class="font-normal text-muted-foreground">({type})</span>
										</span>
										{#if type === 'duration'}
											<!-- Presets, because a duration is the thing an operator actually decides. A
									     free-text box here means learning Go duration syntax from a rejection. -->
											<div class="flex flex-wrap gap-1">
												{#each DURATION_PRESETS as preset (preset)}
													<Button
														size="sm"
														variant={gParams[name] === preset ? 'default' : 'outline'}
														onclick={() => {
															gParams = { ...gParams, [name]: preset };
															preview = null;
														}}
													>
														{preset}
													</Button>
												{/each}
											</div>
										{:else}
											<Input
												value={gParams[name] ?? ''}
												class="font-mono text-xs"
												placeholder={type === 'timestamp' ? 'now (leave blank)' : type}
												aria-label="Condition parameter {name}"
												oninput={(e) => {
													gParams = { ...gParams, [name]: e.currentTarget.value };
													preview = null;
												}}
											/>
											{#if type === 'timestamp'}
												<p class="text-[11px] text-muted-foreground">
													Blank means <span class="font-mono">now</span> — the window opens when you grant.
												</p>
											{/if}
										{/if}
									</div>
								{/each}
							{/if}
						</div>
					{/if}
				{:else}
					<Input
						bind:value={gRelation}
						class="font-mono text-xs"
						placeholder="reader"
						aria-label="Grant relation"
						oninput={() => {
							preview = null;
							previewFailed = false;
						}}
					/>
				{/if}
			</div>
			<!-- ASSERT BEFORE YOU GRANT. Nothing is written: the store evaluates the tuple as a hypothesis
			     and answers with the delta. Offered rather than forced, because a required round trip
			     before every write turns a two-field dialog into a wizard. -->
			<div class="flex flex-col gap-2 rounded-md border border-border p-2">
				<div class="flex items-center justify-between gap-2">
					<span class="text-xs font-medium">Effect</span>
					<Button
						size="sm"
						variant="outline"
						type="button"
						disabled={previewing || !gUser.trim() || !gRelation.trim()}
						onclick={runPreview}
					>
						{previewing ? 'Checking…' : 'Simulate'}
					</Button>
				</div>
				{#if previewFailed}
					<p class="text-[11px] text-muted-foreground">
						Could not simulate — the store did not answer. Granting anyway is still possible; its
						effect is simply unknown.
					</p>
				{:else if preview === null}
					<p class="text-[11px] text-muted-foreground">
						Ask the store what this would change, without writing it.
					</p>
				{:else if preview.baseline}
					<!-- The finding people least expect: in a concentric model the subject often already
					     holds the access by inheritance, so the grant adds a tuple that changes nothing and
					     that someone must later reason about when revoking. -->
					<p class="text-[11px] text-warning">
						<strong>No change.</strong> This subject already holds
						<span class="font-mono">{gRelation}</span> here — inherited, not stored. The tuple would be
						redundant.
					</p>
				{:else if preview.allowed}
					<p class="text-[11px] text-success">
						<strong>Grants access.</strong> Currently denied; after this write
						<span class="font-mono">{gRelation}</span> resolves.
					</p>
				{:else}
					<p class="text-[11px] text-destructive">
						<strong>Would not take effect.</strong> Even with this tuple the relation still does not resolve
						— the model excludes it, or something else is required.
					</p>
				{/if}
			</div>

			<div class="mt-1 flex justify-end gap-2">
				<Button variant="outline" type="button" disabled={busy} onclick={() => (grantOpen = false)}>
					Cancel
				</Button>
				<Button type="submit" disabled={busy || !gUser.trim() || !gRelation.trim()}>
					{busy ? '…' : 'Write tuple'}
				</Button>
			</div>
		</form>
	</Dialog.Content>
</Dialog.Root>

<AlertDialog.Root
	open={revokeTarget !== null}
	onOpenChange={(open) => {
		if (!open) {
			revokeTarget = null;
			blast = null;
		}
	}}
>
	<AlertDialog.Content>
		<AlertDialog.Title>Revoke this tuple</AlertDialog.Title>
		<AlertDialog.Description>
			{#if revokeTarget}
				<span class="font-mono"
					>({revokeTarget.user}, {revokeTarget.relation}, {revokeTarget.object})</span
				>
			{/if}
		</AlertDialog.Description>

		<div class="flex items-start gap-2 rounded border border-warning/50 bg-warning/10 p-2 text-xs">
			<TriangleAlert size={14} class="mt-0.5 shrink-0 text-warning" />
			{#if blastPending}
				<span>Measuring blast radius…</span>
			{:else if blast}
				<span>
					<strong class="tabular-nums">{blast.count}</strong>
					principal{blast.count === 1 ? '' : 's'} currently hold
					<span class="font-mono">{revokeTarget?.relation}</span> here.
					{#if blast.truncated}
						At least — the store truncated the list, so the real number is higher.
					{/if}
				</span>
			{:else}
				<span>
					Could not measure the blast radius. Revoking anyway may remove access for principals this
					dialog cannot name.
				</span>
			{/if}
		</div>

		<div class="mt-2 flex justify-end gap-2">
			<AlertDialog.Cancel disabled={busy}>Cancel</AlertDialog.Cancel>
			<AlertDialog.Action
				class="border-destructive/40 bg-destructive/15 text-destructive hover:bg-destructive/25"
				disabled={busy}
				onclick={revoke}
			>
				Revoke
			</AlertDialog.Action>
		</div>
	</AlertDialog.Content>
</AlertDialog.Root>
