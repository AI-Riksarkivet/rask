<script lang="ts">
	import { Badge } from '@rask/ui/badge';
	import { Button } from '@rask/ui/button';
	import { Card } from '@rask/ui/card';
	import { GatedAction } from '@rask/ui/gated-action';
	import { Input } from '@rask/ui/input';
	import { Textarea } from '@rask/ui/textarea';
	import { formatParams, parseParams, type LaneSpec } from '$lib/lanes';
	import { parseColumns } from '$lib/gates';
	import { clearGate, getGate, setGate } from '$lib/remote/gates.remote';
	import { deleteLane, listLanes, setLane } from '$lib/remote/lanes.remote';

	const lanes = $derived(listLanes());
	const gate = $derived(getGate());
	// The record or null; null means the DEPLOYMENT governs, which is a different statement from a
	// band of zero and is rendered as such.
	const declared = $derived(gate.current?.ok ? gate.current.data : null);

	// SEEDED BY $derived, NOT BY AN EFFECT. Since 5.25 a `$derived` may be reassigned, which is
	// exactly this case: the server's value is the starting point, a keystroke overwrites it, and a
	// change to the record re-seeds. Doing it in an `$effect` needed a `seeded` flag to stop a
	// refresh clobbering what someone was typing — state written from an effect to paper over the
	// absence of a starting value, which is the malpractice the autofixer names.
	//
	// `getGate` is not polled, so the only thing that moves `declared` is this page's own write, and
	// re-seeding from the server's answer after a save is what we want rather than what we guard.
	let gateKeyColumn = $derived(declared?.key_column ?? 'id');
	let gateColumns = $derived((declared?.required_columns ?? []).join(', '));
	let gateBand = $derived(declared ? String(declared.review_band) : '0.25');
	let gateReview = $derived(declared?.review_enabled ?? false);
	let gateBusy = $state(false);
	let gateOutcome = $state<{ tone: 'ok' | 'fail'; text: string } | null>(null);

	const bandNumber = $derived(Number(gateBand));
	const bandValid = $derived(
		gateBand.trim() !== '' && Number.isFinite(bandNumber) && bandNumber >= 0,
	);
	const gateSubmittable = $derived(bandValid && gateKeyColumn.trim() !== '' && !gateBusy);

	function reportGate(result: { ok: boolean; status?: number; detail?: string }, done: string) {
		gateOutcome = result.ok
			? { tone: 'ok', text: done }
			: {
					tone: 'fail',
					text:
						result.status === 403
							? 'Refused: changing a gate needs `can_administer` on this project. A band decides whether a promotion waits for a human, so it is an administrative act.'
							: result.status === 400
								? (result.detail ?? 'No active project.')
								: `Could not save (${result.status}). ${result.detail ?? ''}`.trim(),
				};
	}

	async function saveGate() {
		if (!gateSubmittable) return;
		gateBusy = true;
		gateOutcome = null;
		const result = await setGate({
			key_column: gateKeyColumn.trim(),
			required_columns: parseColumns(gateColumns),
			review_band: bandNumber,
			review_enabled: gateReview,
		});
		gateBusy = false;
		reportGate(
			result,
			`Gate declared — a band of ${bandNumber} governs this project's promotions from the next dispatch. No redeploy.`,
		);
	}

	async function resetGate() {
		if (gateBusy) return;
		gateBusy = true;
		gateOutcome = null;
		const result = await clearGate({});
		gateBusy = false;
		reportGate(
			result,
			'Cleared — the deployment\u2019s own settings govern again. A delete, not a write of zeros.',
		);
	}

	// The draft is local state, not derived from the query: editing an existing lane pre-fills this
	// form, and a refresh landing mid-edit must not overwrite what a person is typing.
	let lane = $state('');
	let fromId = $state('');
	let toId = $state('');
	let entrypoint = $state('');
	let codeVersion = $state('');
	let paramsText = $state('');

	/** Held as a const rather than an inline literal: the placeholder is two LINES, and a mustache
	 *  string literal in the markup is the one form oxfmt and the autofixer both flag. */
	const paramsPlaceholder = 'MODEL=base\nBATCH=32';
	let busy = $state(false);
	let outcome = $state<{ tone: 'ok' | 'fail'; text: string } | null>(null);

	const parsedParams = $derived(parseParams(paramsText));
	const complete = $derived(
		lane.trim() !== '' && fromId.trim() !== '' && toId.trim() !== '' && entrypoint.trim() !== '',
	);
	const submittable = $derived(complete && parsedParams.bad.length === 0 && !busy);

	function edit(spec: LaneSpec) {
		lane = spec.lane;
		fromId = spec.from_id;
		toId = spec.to_id;
		entrypoint = spec.entrypoint;
		codeVersion = spec.code_version;
		paramsText = formatParams(spec.params);
		outcome = null;
	}

	function reset() {
		lane = fromId = toId = entrypoint = codeVersion = paramsText = '';
		outcome = null;
	}

	/** Both writes land here so the three outcomes a person must tell apart — in flight, refused,
	 *  landed — are expressed once. A 403 is NOT an outage: it names the relation that was missing,
	 *  which turns a dead end into a request they can make of someone. */
	function report(result: { ok: boolean; status?: number; detail?: string }, done: string) {
		if (result.ok) {
			outcome = { tone: 'ok', text: done };
			return;
		}
		outcome = {
			tone: 'fail',
			text:
				result.status === 403
					? 'Refused: declaring a lane needs `can_administer` on this project. A lane names an entrypoint that will EXECUTE on the shared Ray cluster against this tenant’s data, so it is an administrative act.'
					: result.status === 400
						? (result.detail ?? 'No active project.')
						: result.status === 422
							? `That lane name does not resolve. ${result.detail ?? ''}`.trim()
							: `Could not save (${result.status}). ${result.detail ?? ''}`.trim(),
		};
	}

	async function save() {
		if (!submittable) return;
		busy = true;
		outcome = null;
		const result = await setLane({
			lane: lane.trim(),
			from_id: fromId.trim(),
			to_id: toId.trim(),
			entrypoint: entrypoint.trim(),
			params: parsedParams.params,
			code_version: codeVersion.trim(),
		});
		busy = false;
		report(
			result,
			`Declared “${lane.trim()}” — a mover set to MEDALLION_LANE=${lane.trim()} now resolves this record instead of its Deployment env.`,
		);
	}

	async function remove(name: string) {
		if (busy) return;
		busy = true;
		outcome = null;
		const result = await deleteLane({ lane: name });
		busy = false;
		report(
			result,
			`Deleted “${name}”. A mover still naming it will now REFUSE at the submit seam rather than silently fall back to its env.`,
		);
	}
</script>

<svelte:head><title>Transform lanes · lance</title></svelte:head>

<div class="flex flex-col gap-4 p-4">
	<div>
		<h1 class="text-lg font-semibold">Transform lanes</h1>
		<p class="text-muted-foreground text-sm">
			A lane is one governed medallion edge — read a table, run an entrypoint, write another. This
			is the door that declares what a lane runs, so it changes here, audited and admin-gated,
			rather than by editing a Deployment. Ray is one of two ways to execute a lane, not what a lane
			is.
		</p>
	</div>

	{#if lanes.error}
		<Card class="p-4">
			<p class="text-destructive text-sm">The catalog could not be reached.</p>
		</Card>
	{:else if lanes.loading}
		<p class="text-muted-foreground text-sm">Loading lanes…</p>
	{:else if lanes.current && !lanes.current.ok}
		<Card class="p-4">
			<p class="text-destructive text-sm">
				{lanes.current.status === 403
					? 'You do not hold `can_administer` on this project, so its lanes cannot be listed. This is a denial, not an empty estate.'
					: lanes.current.status === 404
						? 'The catalog answered 404 for the lane door. That is the DOOR being absent, not this project: a catalog build predating the transform endpoints serves no /transform routes at all. Check the deployed catalog image before reading this as "no lanes".'
						: (lanes.current.detail ?? `Could not list lanes (${lanes.current.status}).`)}
			</p>
		</Card>
	{:else if lanes.current}
		{@const rows = lanes.current.data.transforms}
		<Card class="flex flex-col gap-3 p-4">
			<div class="flex items-center gap-2">
				<h2 class="font-medium">Declared</h2>
				<Badge variant="secondary">{rows.length}</Badge>
			</div>

			{#if rows.length === 0}
				<p class="text-muted-foreground text-sm">
					No lane is declared. Every mover is still running whatever its Deployment env names —
					which nothing here can enumerate, review or gate.
				</p>
			{:else}
				<div class="flex flex-col divide-y">
					{#each rows as spec (spec.lane)}
						<div class="flex flex-col gap-1 py-3 first:pt-0 last:pb-0">
							<div class="flex flex-wrap items-center gap-2">
								<span class="font-mono text-sm font-medium">{spec.lane}</span>
								<span class="text-muted-foreground font-mono text-xs">
									{spec.from_id} → {spec.to_id}
								</span>
								{#if spec.code_version}
									<Badge variant="outline">{spec.code_version}</Badge>
								{/if}
							</div>
							<code class="text-muted-foreground text-xs break-all">{spec.entrypoint}</code>
							{#if Object.keys(spec.params).length > 0}
								<span class="text-muted-foreground text-xs">
									{Object.keys(spec.params).length} param(s), forwarded as RASK_PARAM_*
								</span>
							{/if}
							<div class="flex gap-2 pt-1">
								<!-- The other direction of the same link. A declaration is only useful next to what
								     it produced, and this zone already watches those runs. -->
								<Button
									variant="outline"
									size="sm"
									href={`/compute/jobs?lane=${encodeURIComponent(spec.lane)}`}>Runs</Button
								>
								<Button variant="outline" size="sm" onclick={() => edit(spec)}>Edit</Button>
								<GatedAction allowed={!busy} action="Delete" reason="A write is already in flight.">
									<Button variant="destructive" size="sm" onclick={() => remove(spec.lane)}>
										Delete
									</Button>
								</GatedAction>
							</div>
						</div>
					{/each}
				</div>
			{/if}
		</Card>
	{/if}

	<!-- THE GATE, beside the lanes: both are project-level, so one config surface rather than a
	     second nav leaf. A lane says WHAT runs; the gate says whether its output may publish. -->
	<Card class="flex flex-col gap-3 p-4">
		<div class="flex items-center gap-2">
			<h2 class="font-medium">Quality gate</h2>
			{#if declared}
				<Badge variant="secondary">declared</Badge>
			{:else}
				<Badge variant="outline">deployment defaults</Badge>
			{/if}
		</div>
		<p class="text-muted-foreground text-sm">
			Decides whether a stage's output may publish. Until this door existed every value here was env
			on a mover pod, so moving a threshold meant a values-file edit and a redeploy.
			{#if !declared}
				Nothing is declared, so this project runs on its deployment's own settings — the values
				below are what would be saved, not what is in force.
			{/if}
		</p>

		<div class="grid gap-3 sm:grid-cols-2">
			<label class="flex flex-col gap-1 text-sm">
				<span>Key column</span>
				<Input bind:value={gateKeyColumn} placeholder="id" />
			</label>
			<label class="flex flex-col gap-1 text-sm">
				<span>Review band</span>
				<Input bind:value={gateBand} placeholder="0.25" />
				<span class="text-muted-foreground text-xs">
					A ratio, not a row count: 0.25 means a quarter. A promotion whose row count moves further
					than this waits for a human.
				</span>
			</label>
		</div>

		<label class="flex flex-col gap-1 text-sm">
			<span
				>Required columns <span class="text-muted-foreground">(comma or newline separated)</span
				></span
			>
			<Input bind:value={gateColumns} placeholder="id, payload" />
			<span class="text-muted-foreground text-xs">
				Each becomes a `column_declared` assertion, so a transform that quietly stops emitting one
				is caught here rather than by whoever consumes it.
			</span>
		</label>

		<label class="flex items-center gap-2 text-sm">
			<input type="checkbox" bind:checked={gateReview} class="accent-primary" />
			<span>Hold a breach for a human</span>
			<span class="text-muted-foreground text-xs">
				Off means a breach is logged and published anyway.
			</span>
		</label>

		{#if !bandValid && gateBand.trim() !== ''}
			<p class="text-destructive text-sm">The band must be a number and cannot be negative.</p>
		{/if}

		<div class="flex gap-2">
			<GatedAction
				allowed={gateSubmittable}
				action="Save gate"
				reason={gateBusy
					? 'A write is already in flight.'
					: 'A key column and a non-negative band are required.'}
			>
				<Button onclick={saveGate}>{gateBusy ? 'Saving…' : 'Save gate'}</Button>
			</GatedAction>
			<GatedAction
				allowed={!gateBusy && declared !== null}
				action="Use deployment defaults"
				reason={gateBusy
					? 'A write is already in flight.'
					: 'Nothing is declared — the deployment already governs.'}
			>
				<Button variant="outline" onclick={resetGate}>Use deployment defaults</Button>
			</GatedAction>
		</div>

		{#if gateOutcome}
			<p class={gateOutcome.tone === 'ok' ? 'text-sm' : 'text-destructive text-sm'}>
				{gateOutcome.text}
			</p>
		{/if}
	</Card>

	<Card class="flex flex-col gap-3 p-4">
		<h2 class="font-medium">Declare a lane</h2>
		<p class="text-muted-foreground text-sm">
			Saving an existing name REPLACES that record — the door is an upsert, so this form is both
			create and edit.
		</p>

		<div class="grid gap-3 sm:grid-cols-2">
			<label class="flex flex-col gap-1 text-sm">
				<span>Lane</span>
				<Input bind:value={lane} placeholder="bronze-to-silver" />
			</label>
			<label class="flex flex-col gap-1 text-sm">
				<span>Code version <span class="text-muted-foreground">(optional)</span></span>
				<Input bind:value={codeVersion} placeholder="main-3020c39c" />
			</label>
			<label class="flex flex-col gap-1 text-sm">
				<span>Reads from</span>
				<Input bind:value={fromId} placeholder="bronze$events" />
			</label>
			<label class="flex flex-col gap-1 text-sm">
				<span>Writes to</span>
				<Input bind:value={toId} placeholder="silver$features" />
			</label>
		</div>

		<label class="flex flex-col gap-1 text-sm">
			<span>Entrypoint</span>
			<Input bind:value={entrypoint} placeholder="python /home/ray/jobs/ray_stage_job.py" />
			<span class="text-muted-foreground text-xs">
				Must reference a script BAKED INTO the cluster image. An entrypoint the image lacks dies
				exit 2, and the stage reports FAILED with nothing naming the image.
			</span>
		</label>

		<label class="flex flex-col gap-1 text-sm">
			<span>Parameters <span class="text-muted-foreground">(one KEY=value per line)</span></span>
			<Textarea bind:value={paramsText} rows={4} placeholder={paramsPlaceholder} />
			<span class="text-muted-foreground text-xs">
				Forwarded into the job as RASK_PARAM_*. The platform never reads them. NEVER put a
				credential here — this record is readable by anyone who can read the lane; a workload
				resolves secrets from the Dapr secret store.
			</span>
		</label>

		{#if parsedParams.bad.length > 0}
			<p class="text-destructive text-sm">
				Not KEY=value: {parsedParams.bad.join(', ')}
			</p>
		{/if}

		<div class="flex gap-2">
			<GatedAction
				allowed={submittable}
				action="Save lane"
				reason={busy
					? 'A write is already in flight.'
					: parsedParams.bad.length > 0
						? 'Every parameter line must read KEY=value.'
						: 'Lane, both tables and an entrypoint are required.'}
			>
				<Button onclick={save}>{busy ? 'Saving…' : 'Save lane'}</Button>
			</GatedAction>
			<Button variant="outline" onclick={reset}>Clear</Button>
		</div>

		{#if outcome}
			<p class={outcome.tone === 'ok' ? 'text-sm' : 'text-destructive text-sm'}>{outcome.text}</p>
		{/if}
	</Card>
</div>
