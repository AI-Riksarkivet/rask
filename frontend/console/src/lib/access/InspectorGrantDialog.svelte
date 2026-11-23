<script lang="ts">
	// The inspector's grant dialog: one relationship tuple written to the live store, its relation
	// offered from the model, optionally time-boxed by a model condition, and its effect simulated
	// before anything is written.
	import { Button } from '@riksarkivet/ui/button';
	import { Checkbox } from '@riksarkivet/ui/checkbox';
	import { Dialog } from '@riksarkivet/ui/dialog';
	import { Input } from '@riksarkivet/ui/input';
	import { Select } from '@riksarkivet/ui/select';
	import { namedObject } from '../access';
	import { writeTuple } from '../remote/access.api';
	import GrantEffect from './GrantEffect.svelte';

	type Props = {
		open: boolean;
		/** The selected object, its uuid object as OpenFGA holds it. */
		selected: string | null;
		/** The name the selected table or namespace holds now. */
		selectedName: string | null;
		/** Relations the MODEL says are directly assignable on the selected object's type. A grant can
		 *  only ever name one of these — a derived `can_*` is rejected by the catalog — so the dialog
		 *  offers exactly them instead of a free-text box that learns the rule from a 400. */
		assignableRelations: readonly string[];
		/** The signed-in principal key, so "grant to me" is one click and nobody has to type their own
		 *  `<idp-id>~<user id>`. */
		mySub: string | null;
		/** `{condition: {parameter: type}}` from the compiled model. Drives the TYPED parameter inputs —
		 *  a hand-kept copy would drift the moment the model gains a parameter, and a missing CEL operand
		 *  is a 503, not a soft failure. */
		modelConditions: Record<string, Record<string, string>>;
		/** Called with the write's outcome; the dialog has closed by then when it succeeded. */
		ondone: (message: { ok: boolean; text: string }) => Promise<void>;
	};

	let {
		open = $bindable(false),
		selected,
		selectedName,
		assignableRelations,
		mySub,
		modelConditions,
		ondone,
	}: Props = $props();

	const uid = $props.id();

	let busy = $state(false);
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

	/** Whether the chosen condition has what the grant must carry. A blank `timestamp` is filled with
	 *  the moment of writing (see `grant`), so only the other parameters must be given; a time-boxed
	 *  grant sent without its duration is a write the catalog refuses with a 400. The catalog still
	 *  decides every write: this only keeps the dialog from sending one it knows is incomplete. */
	const conditionReady = $derived(
		!gCondition ||
			conditionParams.every(
				([name, type]) => type === 'timestamp' || (gParams[name] ?? '').trim().length > 0,
			),
	);

	async function grant(): Promise<void> {
		const t = {
			user: gUser.trim(),
			relation: gRelation.trim(),
			object: selected ?? '',
			name: selectedName,
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
		// The form also submits on Enter, so the button's guard is repeated here.
		if (busy || !conditionReady || !t.user || !t.relation || !t.object) return;
		busy = true;
		try {
			const res = await writeTuple(t);
			if (res.ok) {
				open = false;
				gUser = gRelation = gCondition = '';
				gParams = {};
			}
			await ondone(
				res.ok
					? {
							ok: true,
							text: `Wrote (${t.user}, ${t.relation}, ${namedObject(t.object, t.name)}).`,
						}
					: { ok: false, text: res.status === 403 ? 'Denied: estate-admin only.' : res.detail },
			);
		} finally {
			busy = false;
		}
	}
</script>

<Dialog.Root bind:open>
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
								gUser = mySub ?? '';
							}}
						>
							use my identity
						</Button>
					{/if}
				</div>
				<Input
					bind:value={gUser}
					class="font-mono text-xs"
					placeholder="&lt;idp-id&gt;~&lt;user id&gt;"
					aria-label="Grant subject"
				/>
				<!-- The single most confusing thing about this store, stated rather than implied: a person
				     is a PRINCIPAL, the identity provider that vouched for them and the id it reports
				     (LH-063), not a username, and the catalog refuses a bare name no token could prove. -->
				<p class="text-[11px] text-muted-foreground">
					<span class="font-mono">&lt;idp-id&gt;~&lt;user id&gt;</span> — not a username. Or a
					userset:
					<span class="font-mono">role:&lt;id&gt;#assignee</span>.
				</p>
			</div>

			<div class="flex flex-col gap-1">
				<span class="text-xs font-medium">Relation</span>
				{#if assignableRelations.length}
					<!-- Driven by the MODEL, so an ungrantable relation is unrepresentable rather than merely
					     discouraged. A derived `can_*` is computed and the catalog refuses to write it; a
					     free-text box taught that rule via a 400 one round trip later. -->
					<Select
						bind:value={gRelation}
						ariaLabel="Grant relation"
						placeholder="choose a relation…"
						options={assignableRelations.map((r) => ({ value: r, label: r }))}
					/>
					<p class="text-[11px] text-muted-foreground">
						Only directly-assignable relations on
						<span class="font-mono">{selected?.split(':')[0]}</span>. A derived
						<span class="font-mono">can_*</span> is computed by the model and cannot be granted.
					</p>

					<!-- ── time-boxed grants ──────────────────────────────────────────────────────────────────
			     A grant that expires on its own. The failure this kills is the contractor who still has
			     `writer` on bronze eight months after the engagement ended, because revoking was a
			     separate task nobody owned. The condition list and its parameters come from the MODEL
			     (GET /management/v1/access/model), so an unsupported condition or a missing operand is
			     unrepresentable here rather than a 400 one round trip later. -->
					{#if conditionChoices.length}
						<div class="flex flex-col gap-1 rounded-md border border-border p-2">
							<!-- A div and `for`, not a wrapping <label>: the checkbox is a <button>, a labelable
							     element, so a wrapping label would forward the click and toggle it twice. -->
							<div class="flex items-center gap-2 text-xs font-medium">
								<Checkbox
									id="{uid}-time-box"
									checked={gCondition !== ''}
									aria-label="Time-box this grant"
									onCheckedChange={(checked) => {
										gCondition = checked ? (conditionChoices[0] ?? '') : '';
										gParams = {};
									}}
								/>
								<label for="{uid}-time-box">Time-box this grant</label>
							</div>

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
					/>
				{/if}
			</div>
			<!-- Keyed by the grant it describes: any edit to the subject, the relation, the window or the
			     object drops a simulated answer, which spoke of the grant as it was. -->
			{#key JSON.stringify([selected, gUser, gRelation, gCondition, gParams])}
				<GrantEffect user={gUser} relation={gRelation} object={selected} ready={conditionReady} />
			{/key}

			<div class="mt-1 flex justify-end gap-2">
				<Button variant="outline" type="button" disabled={busy} onclick={() => (open = false)}>
					Cancel
				</Button>
				<Button
					type="submit"
					disabled={busy || !conditionReady || !gUser.trim() || !gRelation.trim()}
				>
					{busy ? '…' : 'Write tuple'}
				</Button>
			</div>
		</form>
	</Dialog.Content>
</Dialog.Root>
