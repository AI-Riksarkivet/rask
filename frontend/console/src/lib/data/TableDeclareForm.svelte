<script lang="ts">
	// The tables registry's #85 declare form, and its second half. Declaring reserves an id and writes no
	// bytes, and the append door (`insertRows`) opens the table's dataset to coerce a batch, so it 404s
	// for a table that has none yet. The rows field sends the same submit through the catalog's create
	// door instead, which lands the first data version into a declared-only table's already-reserved
	// location — so declare and fill are two visits to one form rather than a dead end. This is the ONLY
	// surface where that can live: a declared-only table has no dataset to describe, so its detail page
	// is not reachable.
	//
	// Mounted with the registry and shown behind its header's toggle, so the draft and the last outcome
	// survive closing the form.
	import { Button } from '@riksarkivet/ui/button';
	import { Input } from '@riksarkivet/ui/input';
	import { Textarea } from '@riksarkivet/ui/textarea';
	import { missingFieldsHint } from '#lib/form-hint.ts';
	import { createTableWithRows } from './catalog';
	import { parseJsonRows, rowsToArrow } from './json-rows';
	import { declareTable, fetchTables } from './remote/catalog.api';
	import { WriteKey } from './write-key.svelte';

	let {
		open,
		onclose,
		onchanged,
	}: {
		open: boolean;
		/** Escape in any of the form's controls closes it, as it closes every other panel. */
		onclose: () => void;
		/** Read the registry again: the write added a table. */
		onchanged: () => Promise<void>;
	} = $props();

	// What a BARE declare leaves behind, said where the user decides to make one — the dead end was
	// not only missing a door, it was silent about being a dead end.
	const DECLARE_ONLY_NOTE =
		'Left empty, this reserves the name and writes no data: the table has no version and no schema until its first write, and appending rows to it is refused. Its page, or this form with the same namespace and name, lands that first version.';

	let declNs = $state('');
	let declName = $state('');
	let declLocation = $state(''); // optional — empty means the catalog picks the location
	let declRows = $state(''); // optional — a JSON array of row objects turns this into a create
	// Kept while an unanswered create of the same table and rows is retried, so the catalog creates it once (LH-577).
	const createKey = new WriteKey();
	let busy = $state(false);
	let message = $state<{ ok: boolean; text: string } | null>(null);
	const hasRows = $derived(declRows.trim().length > 0);
	const hint = $derived(
		missingFieldsHint(hasRows ? 'Creating' : 'Declaring', [
			['a namespace', declNs],
			['a table name', declName],
		]),
	);

	/** The refusal wording both submit paths share — one denial, worded once. */
	function fail(verb: string, ns: string, status: number, detail: string): void {
		if (status === 401) message = { ok: false, text: `Sign in to ${verb} a table.` };
		else if (status === 403)
			message = {
				ok: false,
				text: `Denied: ${verb === 'create' ? 'creating' : 'declaring'} in ${ns} needs create access (can_create_table).`,
			};
		else if (status === 0 && verb === 'create')
			message = {
				ok: false,
				text: `Not confirmed: the catalog did not answer, so the table may have been created. Creating again sends the same request, which the catalog applies once. (${detail})`,
			};
		else if (status === 0)
			message = { ok: false, text: `Catalog unreachable — the ${verb} was not applied.` };
		else message = { ok: false, text: detail };
	}

	function clear(): void {
		declNs = '';
		declName = '';
		declLocation = '';
		declRows = '';
	}

	async function runDeclare(): Promise<void> {
		const ns = declNs.trim();
		const name = declName.trim();
		if (busy || !ns || !name) return;
		busy = true;
		message = null;
		try {
			const res = await declareTable({
				namespace: ns,
				name,
				location: declLocation.trim() || undefined,
			});
			if (res.ok) {
				message = {
					ok: true,
					// The reservation is real but EMPTY, and saying only "declared" is what sent people to a
					// 404 detail page next. State what exists and what does not.
					text: `declared ${ns}$${name}${res.data.location ? ` @ ${res.data.location}` : ''} — no data yet. ${DECLARE_ONLY_NOTE}`,
				};
				clear();
				await onchanged(); // pull the declared table into the registry
			} else fail('declare', ns, res.status, res.detail);
		} catch (err) {
			// the parse boundary throws on a wire-contract drift — surface it, never render from a lie
			message = { ok: false, text: `declare response drifted from the contract: ${String(err)}` };
		} finally {
			busy = false;
		}
	}

	/** The first write: create the table FROM the typed rows. Serves a brand-new id and a previously
	 *  declared one identically — the catalog lands the first data version into whichever location the
	 *  id already holds, which is why `location` is not sent here and is ignored when rows are given. */
	async function runCreateWithRows(): Promise<void> {
		const ns = declNs.trim();
		const name = declName.trim();
		if (busy || !ns || !name) return;
		const parsed = parseJsonRows(declRows);
		if (!parsed.ok) {
			message = { ok: false, text: `Invalid rows: ${parsed.reason}` };
			return;
		}
		const rows = parsed.rows;
		busy = true;
		message = null;
		try {
			// Browser-side Arrow-IPC encode: the inferred schema BECOMES the table's schema on a create,
			// so whole numbers are written as integers (`rowsToArrow`).
			const arrow = rowsToArrow(rows);
			const res = await createTableWithRows({
				table: `${ns}$${name}`,
				arrow,
				key: createKey.take(JSON.stringify([ns, name, declRows])),
			});
			createKey.settle(res);
			if (res.ok) {
				message = {
					ok: true,
					text: `Created ${ns}$${name} with ${rows.length} row${rows.length === 1 ? '' : 's'}.`,
				};
				clear();
				// EXPLICIT refresh, unlike the declare path. `declareTable` is a command that single-flights
				// `fetchTables().refresh()`; this write is a keep-bytes catalog door route, so nothing
				// invalidates the read for it, and a re-read alone would answer from the cached list until
				// something else advanced the live cursor. (Caught by the e2e, which was flaky exactly as
				// often as the cursor ticked.)
				await fetchTables().refresh();
				await onchanged();
			} else fail('create', ns, res.status, res.detail);
		} catch (e) {
			message = { ok: false, text: `Encode failed: ${e instanceof Error ? e.message : String(e)}` };
		} finally {
			busy = false;
		}
	}

	/** The form opens on a click that asked for it, so its first field takes the focus. */
	function focusOnOpen(input: HTMLInputElement): void {
		input.focus();
	}

	function closeOnEscape(e: KeyboardEvent): void {
		if (e.key !== 'Escape') return;
		e.preventDefault();
		onclose();
	}

	function submit(): void {
		if (hasRows) void runCreateWithRows();
		else void runDeclare();
	}

	const FIELD = 'h-7 font-mono text-xs';
</script>

{#if open}
	<!-- #85 the browser-shaped create. Empty rows → declare (JSON, no Arrow): the id is reserved and
	     nothing is written. Rows → create: the browser encodes them to Arrow IPC and the catalog lands
	     the table's first data version, whether or not the id was declared earlier. -->
	<form
		class="mb-3 flex flex-col gap-2"
		aria-label="Declare table"
		onsubmit={(e) => {
			e.preventDefault();
			submit();
		}}
	>
		<div class="flex flex-wrap gap-2">
			<Input
				class="{FIELD} w-56"
				bind:value={declNs}
				placeholder="namespace — warehouse$name"
				aria-label="Namespace"
				onkeydown={closeOnEscape}
				{@attach focusOnOpen}
			/>
			<Input
				class="{FIELD} w-44"
				bind:value={declName}
				placeholder="table name"
				aria-label="Table name"
				onkeydown={closeOnEscape}
			/>
			<Input
				class="{FIELD} min-w-56 flex-1"
				bind:value={declLocation}
				placeholder="location (optional — catalog picks)"
				aria-label="Location"
				onkeydown={closeOnEscape}
				disabled={hasRows}
				title={hasRows
					? 'Ignored when rows are given: the create lands into the location the id already holds.'
					: undefined}
			/>
		</div>
		<Textarea
			class="min-h-14 resize-y font-mono text-xs"
			bind:value={declRows}
			placeholder={'rows (optional) — [{ "id": 1, "name": "a" }]'}
			aria-label="Initial rows"
			onkeydown={closeOnEscape}
		/>
		<p class="text-muted-foreground m-0 text-xs">
			{hasRows
				? 'The rows are encoded to Arrow and become the schema and first version of this table.'
				: DECLARE_ONLY_NOTE}
		</p>
		<div class="flex flex-wrap items-center gap-2">
			<Button
				size="sm"
				type="submit"
				disabled={busy || hint !== null}
				aria-describedby={hint ? 'declare-hint' : undefined}
				onkeydown={closeOnEscape}
			>
				{busy ? '…' : hasRows ? 'Create with rows' : 'Declare'}
			</Button>
			{#if hint}<span class="text-muted-foreground text-xs" id="declare-hint">{hint}</span>{/if}
		</div>
	</form>
{/if}
{#if message}
	<div class={['banner', { ok: message.ok, fail: !message.ok }]}>{message.text}</div>
{/if}

<style>
	.banner {
		padding: 8px 12px;
		border-radius: var(--radius-sm);
		border: 1px solid var(--border);
		margin-bottom: 12px;
		font-size: 13px;
	}
	.banner.ok {
		border-color: color-mix(in srgb, var(--success) 45%, var(--border));
		color: var(--success);
	}
	.banner.fail {
		border-color: color-mix(in srgb, var(--destructive) 45%, var(--border));
		color: var(--destructive);
	}
</style>
