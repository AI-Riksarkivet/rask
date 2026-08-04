<script lang="ts">
	// `/tables/<id>` — the catalog table-detail view (#52): schema, stats, version + tag history, the
	// #50 maintenance policy (owner-gated set/delete), and the #51 access review. Data comes in one
	// round-trip through the `fetchTableDetail` remote query (the six-read fan-out still happens on the
	// zone server); every write is a remote command carrying the signed-in user's bearer. A dataset the
	// catalog does not register (e.g. a storage-managed medallion zone) renders the honest
	// not-in-catalog state instead of a broken page.
	import { AlertDialog } from '@rask/ui/alert-dialog';
	import { GrantsPanel, type GrantsClient } from '@rask/ui/grants-panel';
	import { Select } from '@rask/ui/select';
	import { Database, RefreshCw, ShieldAlert, Trash2, Undo2 } from '@lucide/svelte';
	import { Button } from '@rask/ui/button';
	import { Subject } from '@rask/ui/identity';
	import { tableFromJSON, tableToIPC } from 'apache-arrow';
	import { goto } from '$app/navigation';
	import { base } from '$app/paths';
	import { page } from '$app/state';
	import AccessGraph from './AccessGraph.svelte';
	import { fetchProducers } from '$lib/api';
	import type { ProducerInfo } from '@rask/api/lineage';
	import { bffPath } from '$lib/http';
	import {
		checkAccess,
		fetchAccess,
		grantAccess,
		revokeAccess,
	} from './remote/access-objects.remote';
	import { deriveQuality, type QualityBadge } from '$lib/quality';
	import ReadersPanel from '$lib/ReadersPanel.svelte';
	import {
		type GcPreview,
		insertRows,
		partErrored,
		type Policy,
		RETYPE_TYPES,
		type TableStats,
		type TableDetail,
	} from './catalog';
	import { policyRequestFrom } from './namespace';
	import {
		addColumn,
		backfillColumn,
		compactTable,
		createTableIndex,
		deleteRows,
		deleteTablePolicy,
		deregisterTable,
		dropColumn,
		dropTable,
		dropTableIndex,
		fetchTableDetail,
		fetchTableTasks,
		previewMaintenance,
		renameColumn,
		renameTable,
		retypeColumn,
		runMaintenance,
		setTablePolicy,
		undropTable,
		updateRows,
		type TrashEntry,
	} from './remote/catalog.remote';
	import DetailTabs from './DetailTabs.svelte';
	import { fmtBytes, fmtEpoch } from './history';
	import TableHistory from './TableHistory.svelte';
	import StageBadge from './StageBadge.svelte';
	import { stageOfTable } from './stage';
	import TablePreview from './TablePreview.svelte';
	import TableProperties from './TableProperties.svelte';

	let { table }: { table: string } = $props();

	// Goal cond 3: the FGA view lives on an Access TAB (overview stays the default); the medallion
	// stage badge is derived from the table's namespace segment. Goal cond 5 adds the Preview tab.
	// #113 adds History — the commit log, which took the version/branch/tag surface off overview.
	// `?tab=` deep-links a tab (the registry drawer's "access" jump uses it) and the tab bar now WRITES it,
	// so a tab is linkable, reload-stable and back-button-able.
	const TABS = ['overview', 'preview', 'history', 'access'];
	// DERIVED from the URL, not mirrored into it: the tab is a linkable, reload-stable, back-button-able
	// piece of the address, and deriving it is the only way that cannot race. (Mirroring `tab` into the URL
	// from an effect left the deep link at the mercy of effect order — on reload the mirror stripped
	// `?tab=history` before the reader saw it, and the page opened on overview.)
	const tab = $derived.by(() => {
		const wanted = page.url.searchParams.get('tab') ?? 'overview';
		return TABS.includes(wanted) ? wanted : 'overview';
	});
	function selectTab(next: string): void {
		const url = new URL(page.url);
		if (next === 'overview') url.searchParams.delete('tab');
		else url.searchParams.set('tab', next);
		goto(url, { replaceState: true, keepFocus: true, noScroll: true });
	}
	const stageInfo = $derived(stageOfTable(table));

	// Return here after the OIDC round-trip (the shell's ?redirect= contract, nav-user.svelte).
	const loginHref = $derived(`/auth/login?redirect=${encodeURIComponent(page.url.pathname)}`);

	// The zone-owned catalog seam the shared @rask/ui GrantsPanel calls (the lib never owns an API
	// client). The panel's positional signature is bound to the remote functions' single argument here.
	const grantsClient: GrantsClient = {
		fetchAccess: (kind, id) => fetchAccess({ kind, id }),
		checkAccess: (kind, id, user, relation) => checkAccess({ kind, id, user, relation }),
		grantAccess: (kind, id, user, relation) => grantAccess({ kind, id, user, relation }),
		revokeAccess: (kind, id, user, relation) => revokeAccess({ kind, id, user, relation }),
	};

	let detail = $state<TableDetail | null>(null);
	let lastStatus = $state(0);
	let busy = $state(false);
	let policyError = $state<string | null>(null);
	let editingPolicy = $state(false);
	// number | null, matching what bind:value on a type="number" input actually delivers (Svelte 5
	// coerces to a number, or null for an empty field) — typing them as string crashed savePolicy's
	// guards the moment the user touched a field (audit 2026-07-16).
	let draft = $state<{
		retention_days: number | null;
		retain_versions: number | null;
		interval: number | null;
		target: number | null; // #76 target_rows_per_fragment
		enabled: boolean;
	}>({ retention_days: null, retain_versions: null, interval: null, target: null, enabled: true });

	// #64 blob preview — the credential-less read of a blob cell (GET /blobs?column=&row=), session-gated
	// by the catch-all BFF (reader-tier can_read_data). Only offered for binary/blob-typed columns.
	let blobCol = $state('');
	let blobRow = $state<number | null>(null);
	let blobSrc = $state<string | null>(null);
	let blobFailed = $state(false);

	// #64 data-plane row insert — append JSON rows, browser-encoded to Arrow via apache-arrow. Writer-gated.
	let insertJson = $state('');
	let insertBusy = $state(false);
	let insertMsg = $state<{ ok: boolean; text: string } | null>(null);

	// #85 row ops — update/delete rows by SQL predicate (writer-gated can_write_data). Update carries the
	// wire's [[column, expression], …] SET list; delete is destructive → two-click confirm (the restore/GC
	// idiom). The affected-row count comes from the update response; the delete wire has no count (only
	// the new version), stated honestly.
	let rowPredicate = $state('');
	let rowSets = $state<{ column: string; expression: string }[]>([{ column: '', expression: '' }]);
	let rowBusy = $state(false);
	let rowMsg = $state<{ ok: boolean; text: string } | null>(null);
	let rowDeleteConfirm = $state(false);
	const rowSetPairs = $derived(
		rowSets
			.map((s) => [s.column.trim(), s.expression.trim()] as [string, string])
			.filter(([column, expression]) => column && expression),
	);
	// A HALF-filled pair (column without expression, or vice versa) must BLOCK the update — silently
	// dropping it would apply a different write than the one on screen (audit 2026-07-23). Fully-empty
	// extra pairs stay ignorable (the "+ add SET pair" affordance always leaves one around).
	const rowSetPartial = $derived(
		rowSets.some((s) => (s.column.trim() === '') !== (s.expression.trim() === '')),
	);

	function rowFail(status: number, detail: string): void {
		if (status === 401) rowMsg = { ok: false, text: 'Sign in to change rows.' };
		else if (status === 403)
			rowMsg = { ok: false, text: 'Denied: row changes need writer access (can_write_data).' };
		else rowMsg = { ok: false, text: detail };
	}

	async function runUpdateRows(): Promise<void> {
		const sets = rowSetPairs;
		if (rowBusy || sets.length === 0 || rowSetPartial) return;
		rowBusy = true;
		rowMsg = null;
		const requested = table; // latest-wins: don't post A's result onto B if the user navigated mid-write
		try {
			const res = await updateRows({
				table: requested,
				predicate: rowPredicate.trim() || null,
				updates: sets,
			});
			if (table !== requested) return;
			if (res.ok) {
				rowMsg = {
					ok: true,
					text: `Updated ${res.data.updated_rows} row${res.data.updated_rows === 1 ? '' : 's'} → v${res.data.version}.`,
				};
				await load(); // the update bumped the version — refresh stats + versions
			} else rowFail(res.status, res.detail);
		} catch (err) {
			// the parse boundary throws on a wire-contract drift — surface it, never render from a lie
			rowMsg = { ok: false, text: `update response drifted from the contract: ${String(err)}` };
		} finally {
			rowBusy = false;
		}
	}

	async function runDeleteRows(): Promise<void> {
		const predicate = rowPredicate.trim();
		if (rowBusy || !predicate) return;
		rowBusy = true;
		rowMsg = null;
		const requested = table;
		try {
			const res = await deleteRows({ table: requested, predicate });
			if (table !== requested) return;
			if (res.ok) {
				// the delete wire carries no row count — surface the new version honestly instead
				rowMsg = {
					ok: true,
					text: `Deleted rows matching the predicate${res.data.version != null ? ` → v${res.data.version}` : ''}.`,
				};
				await load();
			} else rowFail(res.status, res.detail);
		} catch (err) {
			rowMsg = { ok: false, text: `delete response drifted from the contract: ${String(err)}` };
		} finally {
			rowBusy = false;
			rowDeleteConfirm = false; // ALWAYS disarm — success or failure, the confirm must not stay armed
		}
	}

	// #85 backfill values into a column (async native job — the response is a job_id; the version bump is
	// reconciled when the job lands, so there is nothing to refresh here). Writer-gated (can_write_data).
	let backfilling = $state<string | null>(null); // the column currently being backfilled
	let backfillWhere = $state('');
	let backfillMsg = $state<string | null>(null);

	async function runBackfill(): Promise<void> {
		const column = backfilling;
		if (colBusy || !column) return;
		colBusy = true;
		colError = null;
		backfillMsg = null;
		const requested = table; // latest-wins: don't post A's job message onto B if the user navigated mid-write
		try {
			const res = await backfillColumn({
				table: requested,
				column,
				where: backfillWhere.trim() || undefined,
			});
			if (table !== requested) return;
			if (res.ok) {
				backfilling = null;
				backfillWhere = '';
				backfillMsg = `Backfill of ${column} started · job ${res.data.job_id}.`;
			} else colFail(res.status, res.detail);
		} catch (err) {
			colError = `backfill response drifted from the contract: ${String(err)}`;
		} finally {
			colBusy = false;
		}
	}

	// #85 danger zone — rename (navigates to the renamed id), drop + deregister behind an AlertDialog
	// confirm (NamespaceRegistry's pattern, incl. the always-close-in-finally fix).
	let renameTableTo = $state('');
	let dangerBusy = $state(false);
	let dangerError = $state<string | null>(null);
	let dangerOpen = $state(false);
	let dangerAction = $state<'drop' | 'deregister' | null>(null);
	// The `<ns>$` prefix the renamed table keeps — this form renames within the table's own namespace.
	const nsPrefix = $derived(table.includes('$') ? table.slice(0, table.lastIndexOf('$') + 1) : '');

	function openDanger(action: 'drop' | 'deregister'): void {
		dangerAction = action;
		dangerError = null;
		dangerOpen = true;
	}

	function dangerFail(action: string, status: number, detail: string): void {
		if (status === 401) dangerError = `Sign in — ${action} is a per-user action.`;
		else if (status === 403)
			dangerError =
				action === 'deregister'
					? 'Denied: deregistering needs the owner rung (can_deregister).'
					: `Denied: ${action} needs the owner rung (can_drop).`;
		else if (status === 0) dangerError = `Catalog unreachable — the ${action} was not applied.`;
		else dangerError = detail;
	}

	async function confirmDanger(): Promise<void> {
		const action = dangerAction;
		if (action === null || dangerBusy) return;
		dangerBusy = true;
		dangerError = null;
		try {
			const res = action === 'drop' ? await dropTable({ table }) : await deregisterTable({ table });
			if (res.ok) {
				await goto(`${base}/catalog/tables`); // the id no longer names a table — back to the registry
			} else {
				dangerFail(action, res.status, res.detail);
			}
		} catch (err) {
			// the parse boundary throws on a wire-contract drift — surface it, never render from a lie
			dangerError = `${action} response drifted from the contract: ${String(err)}`;
		} finally {
			// ALWAYS close + disarm: bits-ui's AlertDialog.Action does not auto-close, so leaving the dialog
			// open would keep the destructive action armed for a second, confirm-free fire (audit: major).
			dangerBusy = false;
			dangerOpen = false;
			dangerAction = null;
		}
	}

	async function runRenameTable(): Promise<void> {
		const to = renameTableTo.trim();
		if (dangerBusy || !to) return;
		dangerBusy = true;
		dangerError = null;
		const requested = table;
		const newId = `${nsPrefix}${to}`;
		try {
			const res = await renameTable({ table: requested, newName: to });
			if (table !== requested) return;
			if (res.ok) {
				renameTableTo = '';
				// the old id no longer exists — follow the table to its renamed detail page
				await goto(`${base}/catalog/tables/${encodeURIComponent(newId)}`);
			} else if (res.status === 409) {
				dangerError = `Denied: a table named ${newId} already exists.`;
			} else {
				dangerFail('rename', res.status, res.detail);
			}
		} catch (err) {
			dangerError = `rename response drifted from the contract: ${String(err)}`;
		} finally {
			dangerBusy = false;
		}
	}

	// #81 the SvelteFlow authorization graph is lazy-mounted (heavy) behind this toggle.
	let showGraph = $state(false);

	// scope #6 quality gate — the validator's latest dataQualityAssertions verdict for this dataset, from
	// the lineage service's producing runs (medallion stages record it; a plain catalog table has none).
	let quality = $state<QualityBadge>(null);
	// The same runs, kept rather than thrown away: #113's "by whom" column joins them to versions, so the
	// commit log costs no extra request. `null` = the lineage store did not answer at all.
	let producerRuns = $state<ProducerInfo[] | null>(null);

	async function loadQuality(): Promise<void> {
		const current = table;
		const res = await fetchProducers(current);
		if (table !== current) return; // latest-wins
		quality = deriveQuality(res);
		producerRuns = res?.producers ?? null;
	}

	const unauthorized = $derived(detail === null && lastStatus === 401);
	const notInCatalog = $derived(detail === null && lastStatus === 404);

	// #75 recovery. Only asked for on a 404 — the one state where a dropped table's owner lands — so
	// a live table never pays a round-trip for a question that cannot apply to it.
	let recoverable = $state<TrashEntry | null>(null);
	let recovering = $state(false);
	let recoverError = $state<string | null>(null);

	$effect(() => {
		if (!notInCatalog) {
			recoverable = null;
			recoverError = null; // a failed undrop must not follow the user to the NEXT table's 404
			return;
		}
		const current = table;
		void fetchTableTasks(current).then((res) => {
			if (table !== current) return; // latest-wins across navigation
			recoverable = res.ok ? (res.data[0] ?? null) : null;
		});
	});

	async function recover(): Promise<void> {
		if (recovering) return;
		recovering = true;
		recoverError = null;
		try {
			const res = await undropTable(table);
			if (res.ok) {
				recoverable = null;
				await load();
				return;
			}
			// VERBATIM, like every other refusal in this estate — the catalog names the reason (an
			// expired grace period reads differently from a permission denial, and both matter here).
			recoverError = res.detail;
		} finally {
			recovering = false;
		}
	}
	const denied = $derived(detail === null && lastStatus === 403);
	const offline = $derived(detail === null && ![0, 200, 401, 403, 404].includes(lastStatus));

	async function load(): Promise<void> {
		// Latest-wins: the user may navigate table A→B while A's request is in flight (the route reuses
		// this instance), so drop a response for a table we have already navigated away from.
		const requested = table;
		const res = await fetchTableDetail({ table: requested });
		if (table !== requested) return;
		if (res.ok) {
			detail = res.data;
			lastStatus = 200;
		} else {
			lastStatus = res.status;
		}
	}

	$effect(() => {
		// Reset every piece of state on a table change — including the edit form, or an editor opened
		// on A would survive into B and Save would write A's draft to B (audit 2026-07-16).
		void table;
		detail = null;
		lastStatus = 0;
		editingPolicy = false;
		policyError = null;
		busy = false;
		blobCol = '';
		blobRow = null;
		blobSrc = null;
		blobFailed = false;
		insertJson = '';
		insertMsg = null;
		// #85 row ops + danger zone — reset too, or a predicate/SET pair (or an armed delete confirm)
		// typed on table A would pre-fill B's form and fire against B.
		rowPredicate = '';
		rowSets = [{ column: '', expression: '' }];
		rowMsg = null;
		rowDeleteConfirm = false;
		backfilling = null;
		backfillWhere = '';
		backfillMsg = null;
		renameTableTo = '';
		dangerOpen = false;
		dangerAction = null;
		dangerError = null;
		showGraph = false;
		gcDays = null;
		gcKeep = null;
		gcPreview = null;
		gcResult = null;
		gcError = null;
		gcConfirm = false;
		compactResult = null;
		colBusy = false;
		colError = null;
		addColName = '';
		addColExpr = '';
		renaming = null;
		renameTo = '';
		retyping = null;
		retypeTo = '';
		// #73 index-builder editor — reset too, or a column/type/distance (or error) typed on table A pre-fills
		// B's Build-index form and runCreateIndex(B) would use A's column. (audit 2026-07-20)
		ixColumn = '';
		ixType = 'BTREE';
		ixDistance = 'cosine';
		ixBusy = false;
		ixError = null;
		quality = null;
		producerRuns = null;
		load();
		loadQuality();
	});

	function startPolicyEdit(): void {
		draft = {
			retention_days: policy?.retention_days ?? null,
			retain_versions: policy?.retain_versions ?? null,
			interval: policy?.compact_interval_hours ?? null,
			target: policy?.target_rows_per_fragment ?? null,
			enabled: policy?.compact_enabled ?? true,
		};
		policyError = null;
		editingPolicy = true;
	}

	function policyFail(status: number, detailText: string): void {
		if (status === 401) policyError = 'Sign in to edit the maintenance policy.';
		else if (status === 403) policyError = 'Denied: policy changes need the owner rung (can_drop).';
		else policyError = detailText;
	}

	async function savePolicy(): Promise<void> {
		if (busy) return;
		busy = true;
		policyError = null;
		try {
			const body = policyRequestFrom(draft);
			const res = await setTablePolicy({ table, policy: body });
			if (res.ok) {
				editingPolicy = false;
				await load();
			} else {
				policyFail(res.status, res.detail);
			}
		} finally {
			busy = false;
		}
	}

	async function removePolicy(): Promise<void> {
		if (busy) return;
		busy = true;
		policyError = null;
		try {
			const res = await deleteTablePolicy({ table });
			if (res.ok) await load();
			else policyFail(res.status, res.detail);
		} finally {
			busy = false;
		}
	}

	// #75 on-demand GC — preview (dry-run reclaimable versions) + run (destructive, two-click confirm).
	let gcDays = $state<number | null>(null);
	let gcKeep = $state<number | null>(null);
	let gcBusy = $state(false);
	let gcPreview = $state<GcPreview | null>(null);
	let gcResult = $state<string | null>(null);
	let gcError = $state<string | null>(null);
	let gcConfirm = $state(false);
	const gcHasBound = $derived(gcDays != null || gcKeep != null);

	function gcFail(status: number, detail: string): void {
		if (status === 401) gcError = 'Sign in to run garbage collection.';
		else if (status === 403) gcError = 'Denied: GC needs the owner rung (can_drop).';
		else if (status === 422) gcError = 'Set a retention-days and/or keep-last bound first.';
		else gcError = detail;
	}

	async function runGcPreview(): Promise<void> {
		if (gcBusy || !gcHasBound) return;
		gcBusy = true;
		gcError = null;
		gcResult = null;
		gcConfirm = false;
		try {
			const res = await previewMaintenance({
				table,
				bounds: { retention_days: gcDays, retain_versions: gcKeep },
			});
			if (res.ok) gcPreview = res.data;
			else gcFail(res.status, res.detail);
		} finally {
			gcBusy = false;
		}
	}

	async function runGc(): Promise<void> {
		if (gcBusy || !gcHasBound) return;
		gcBusy = true;
		gcError = null;
		try {
			const res = await runMaintenance({
				table,
				bounds: { retention_days: gcDays, retain_versions: gcKeep },
			});
			if (res.ok) {
				gcResult = `Reclaimed ${res.data.old_versions_removed} version(s) · ${fmtBytes(res.data.bytes_removed)}.`;
				gcPreview = null;
				gcConfirm = false;
				await load(); // GC changed the version set — refresh
			} else gcFail(res.status, res.detail);
		} finally {
			gcBusy = false;
		}
	}

	// #74 schema evolution — add (name + SQL expr) / rename / drop columns (writer-gated).
	let colBusy = $state(false);
	let colError = $state<string | null>(null);
	let addColName = $state('');
	let addColExpr = $state('');
	let renaming = $state<string | null>(null); // the column currently being renamed
	let renameTo = $state('');
	let retyping = $state<string | null>(null); // the column currently being re-typed (#74 tail)
	let retypeTo = $state(''); // the target scalar Arrow type (bits-ui Select string)

	function colFail(status: number, detail: string): void {
		if (status === 401) colError = 'Sign in to change the schema.';
		else if (status === 403)
			colError = 'Denied: schema changes need writer access (can_write_data).';
		else colError = detail;
	}

	async function runAddColumn(): Promise<void> {
		const name = addColName.trim();
		const expr = addColExpr.trim();
		if (colBusy || !name || !expr) return;
		colBusy = true;
		colError = null;
		try {
			const res = await addColumn({ table, name, expression: expr });
			if (res.ok) {
				addColName = '';
				addColExpr = '';
				await load();
			} else colFail(res.status, res.detail);
		} finally {
			colBusy = false;
		}
	}

	async function runDropColumn(name: string): Promise<void> {
		if (colBusy) return;
		colBusy = true;
		colError = null;
		try {
			const res = await dropColumn({ table, name });
			if (res.ok) await load();
			else colFail(res.status, res.detail);
		} finally {
			colBusy = false;
		}
	}

	async function runRenameColumn(): Promise<void> {
		const from = renaming;
		const to = renameTo.trim();
		if (colBusy || !from || !to) return;
		colBusy = true;
		colError = null;
		try {
			const res = await renameColumn({ table, path: from, rename: to });
			if (res.ok) {
				renaming = null;
				renameTo = '';
				await load();
			} else colFail(res.status, res.detail);
		} finally {
			colBusy = false;
		}
	}

	async function runRetypeColumn(): Promise<void> {
		const path = retyping;
		const type = retypeTo;
		if (colBusy || !path || !type) return;
		colBusy = true;
		colError = null;
		try {
			const res = await retypeColumn({ table, path, type });
			if (res.ok) {
				retyping = null;
				retypeTo = '';
				await load();
			} else colFail(res.status, res.detail);
		} finally {
			colBusy = false;
		}
	}

	// #76 compact-now — merge small fragments (non-destructive), using the policy's target size if set.
	let compactBusy = $state(false);
	let compactResult = $state<string | null>(null);

	async function runCompact(): Promise<void> {
		if (compactBusy) return;
		compactBusy = true;
		gcError = null;
		compactResult = null;
		try {
			const res = await compactTable({
				table,
				targetRowsPerFragment: policy?.target_rows_per_fragment ?? null,
			});
			if (res.ok) {
				compactResult = `Compacted · ${res.data.fragments_removed} fragment(s) → ${res.data.fragments_added}.`;
				await load(); // compaction wrote a new version — refresh
			} else gcFail(res.status, res.detail);
		} finally {
			compactBusy = false;
		}
	}

	function previewBlob(): void {
		if (!blobCol || blobRow == null) return;
		blobFailed = false;
		// The catch-all BFF forwards the query + the session bearer + the binary body → an <img> src works.
		// base-prefixed so the <img> hits THIS zone's proxy under its base path (not a bare origin /capi).
		blobSrc = bffPath(
			`/capi/v1/table/${encodeURIComponent(table)}/blobs?column=${encodeURIComponent(blobCol)}&row=${blobRow}`,
		);
	}

	async function runInsert(): Promise<void> {
		if (insertBusy || !insertJson.trim()) return;
		insertBusy = true;
		insertMsg = null;
		let rows: Record<string, unknown>[];
		try {
			const parsed: unknown = JSON.parse(insertJson);
			if (!Array.isArray(parsed) || parsed.length === 0) {
				throw new Error('expected a non-empty JSON array of row objects');
			}
			rows = parsed as Record<string, unknown>[];
		} catch (e) {
			insertMsg = {
				ok: false,
				text: `Invalid rows: ${e instanceof Error ? e.message : String(e)}`,
			};
			insertBusy = false;
			return;
		}
		const requested = table; // latest-wins: don't post A's result onto B if the user navigated mid-insert
		try {
			// Browser-side Arrow-IPC encode (apache-arrow) → the catalog's Arrow-body insert. The inferred
			// schema must match the table's — a mismatch is the catalog's honest error, surfaced below.
			const arrow = tableToIPC(tableFromJSON(rows), 'stream');
			const res = await insertRows(requested, arrow);
			if (table !== requested) return; // navigated away — A's message must not land on B's page
			if (res.ok) {
				insertMsg = {
					ok: true,
					text: `Inserted ${rows.length} row${rows.length === 1 ? '' : 's'}.`,
				};
				insertJson = '';
				await load(); // the insert bumped the version — refresh stats + versions
			} else if (res.status === 401) {
				insertMsg = { ok: false, text: 'Sign in to insert rows.' };
			} else if (res.status === 403) {
				insertMsg = {
					ok: false,
					text: 'Denied: inserting rows needs writer access (can_write_data).',
				};
			} else {
				insertMsg = { ok: false, text: res.detail };
			}
		} catch (e) {
			insertMsg = {
				ok: false,
				text: `Encode failed: ${e instanceof Error ? e.message : String(e)}`,
			};
		} finally {
			insertBusy = false;
		}
	}

	// Split each part into "resolved value" vs "upstream failed" so the markup can render an honest
	// "unavailable" instead of an affirmative empty state (which for policy would invite an overwrite).
	const stats = $derived(
		partErrored(detail?.stats) ? null : ((detail?.stats ?? null) as TableStats | null),
	);
	const policy = $derived(
		partErrored(detail?.policy) ? null : ((detail?.policy ?? null) as Policy | null),
	);
	const policyUnavailable = $derived(partErrored(detail?.policy));
	const schemaFields = $derived(
		(detail?.describe.schema?.fields ?? []) as {
			name: string;
			type?: unknown;
			nullable?: boolean;
			metadata?: Record<string, string>;
		}[],
	);
	// #74 tail — the table's schema-level metadata map (what schema_metadata/update sets), for the properties
	// editor. `describe.metadata` is the current map; absent → an empty, still-editable map.
	const tableMeta = $derived((detail?.describe.metadata ?? {}) as Record<string, string>);
	// Columns whose type is binary/blob — the ones the blob preview can read a cell from.
	const blobColumns = $derived(
		schemaFields
			.filter((f) => /binary|blob/i.test(typeName(f.type)))
			.map((f) => f.name)
			.filter((n): n is string => !!n),
	);
	const versions = $derived(
		partErrored(detail?.versions)
			? []
			: ((detail?.versions?.versions ?? []) as {
					version?: number;
					timestamp_millis?: number | null;
					manifest_size?: number | null;
					e_tag?: string | null;
				}[]),
	);
	const tags = $derived(
		partErrored(detail?.tags)
			? []
			: Object.entries((detail?.tags?.tags ?? {}) as Record<string, { version?: number }>),
	);
	// Lance branches: a name → BranchContents map (createAt in seconds, manifestSize in bytes).
	const branches = $derived(
		partErrored(detail?.branches)
			? []
			: Object.entries(
					(detail?.branches?.branches ?? {}) as Record<
						string,
						{ createAt?: number; manifestSize?: number | null }
					>,
				),
	);
	// Indexes on the table (#64) — scalar/vector, each over one or more columns.
	const indexes = $derived(
		partErrored(detail?.indexes)
			? []
			: ((detail?.indexes?.indexes ?? []) as {
					index_name?: string;
					columns?: string[];
					index_type?: string | null;
				}[]),
	);

	function typeName(t: unknown): string {
		if (t && typeof t === 'object' && 'type' in t) return String((t as { type: unknown }).type);
		return String(t ?? '—');
	}

	// #73 index management — build (scalar / vector) + drop. Vector indexes (IVF/HNSW) are the Lance
	// differentiator neither Iceberg nor Unity has; a rebuild is a create with the same column (Lance
	// replaces). The catalog gates all three at the writer tier (can_write_data).
	const SCALAR_TYPES = ['BTREE', 'BITMAP', 'INVERTED', 'NGRAM'];
	const VECTOR_TYPES = ['IVF_PQ', 'IVF_HNSW_SQ'];
	let ixColumn = $state('');
	let ixType = $state('BTREE');
	let ixDistance = $state('cosine');
	let ixBusy = $state(false);
	let ixError = $state<string | null>(null);
	const ixScalar = $derived(SCALAR_TYPES.includes(ixType));

	async function runCreateIndex(): Promise<void> {
		const column = ixColumn.trim();
		if (ixBusy || !column || !ixType) return;
		ixBusy = true;
		ixError = null;
		try {
			const body = ixScalar
				? { column, index_type: ixType }
				: { column, index_type: ixType, distance_type: ixDistance };
			const res = await createTableIndex({ table, body, scalar: ixScalar });
			if (res.ok) {
				ixColumn = '';
				await load(); // pull the new index into the list (the build bumps the version too)
			} else if (res.status === 401) {
				ixError = 'Sign in to build an index.';
			} else if (res.status === 403) {
				ixError = 'Denied: building an index needs writer access (can_write_data).';
			} else {
				ixError = res.detail;
			}
		} finally {
			ixBusy = false;
		}
	}

	async function runDropIndex(name: string | undefined): Promise<void> {
		if (ixBusy || !name) return;
		ixBusy = true;
		ixError = null;
		try {
			const res = await dropTableIndex({ table, name });
			if (res.ok) {
				await load();
			} else if (res.status === 401) {
				ixError = 'Sign in to drop an index.';
			} else if (res.status === 403) {
				ixError = 'Denied: dropping an index needs writer access (can_write_data).';
			} else {
				ixError = res.detail;
			}
		} finally {
			ixBusy = false;
		}
	}
</script>

<div class="page">
	<header>
		<Database size={16} />
		<h1 class="mono">{table}</h1>
		{#if stageInfo}<StageBadge info={stageInfo} />{/if}
		{#if detail?.describe.version != null}
			<span class="sub mono">v{detail.describe.version}</span>
		{/if}
	</header>

	{#if unauthorized}
		<div class="empty">
			<ShieldAlert size={16} />
			<p>
				This stack is governed — <a href={loginHref} data-sveltekit-reload>sign in</a> to view table details.
			</p>
		</div>
	{:else if notInCatalog}
		<!-- #75: a 404 here is exactly where someone lands right after dropping a table, so this is
		     where recovery belongs. `recoverable` is the catalog's own trash record — if one exists the
		     bytes are still there and the deadline is real; if not, the drop was destructive and the
		     honest answer is the plain not-registered copy below. -->
		{#if recoverable}
			<div class="recover">
				<h2>This table was dropped — and is still recoverable</h2>
				<p>
					Its bytes were never deleted. Recovering re-registers them at
					<code class="mono">{recoverable.location}</code>.
				</p>
				<p class="mut">
					Dropped by <Subject value={recoverable.dropped_by} />
					on {recoverable.dropped_at.slice(0, 10)} · recoverable until
					<strong>{recoverable.expires_at.slice(0, 10)}</strong>, after which the maintenance sweep
					reports it for reclamation.
				</p>
				{#if recoverError}<p class="problem" data-testid="undrop-problem">{recoverError}</p>{/if}
				<Button size="sm" disabled={recovering} onclick={recover}>
					<Undo2 size={14} />
					{recovering ? 'Recovering…' : 'Undrop this table'}
				</Button>
			</div>
		{:else}
			<div class="empty">
				<p>
					Not a catalog-registered table — storage-managed datasets (medallion zones) have no catalog
					detail. Its lineage is on the <a href="/lakehouse/lineage" data-sveltekit-reload>explorer</a>.
				</p>
			</div>
		{/if}
	{:else if denied}
		<div class="empty">
			<ShieldAlert size={16} />
			<p>
				You don't have read access to this table's catalog metadata — its lineage is on the <a
					href="/lakehouse/lineage"
					data-sveltekit-reload>explorer</a
				>.
			</p>
		</div>
	{:else if offline}
		<div class="empty">
			<RefreshCw size={16} />
			<p>Catalog unreachable (HTTP {lastStatus}).</p>
		</div>
	{:else if detail === null}
		<div class="empty"><p>Loading…</p></div>
	{:else}
		<!-- Goal cond 3: overview (default) | preview (goal cond 5) | history (#113) | access. -->
		<DetailTabs tabs={TABS} active={tab} onselect={selectTab} />
		{#if tab === 'preview'}
			<TablePreview {table} />
		{:else if tab === 'history'}
			<!-- #113 the commit log. `producers` is the SAME fetch the quality badge already made, so the
			     author join costs no extra request. Keyed by table so a navigation REMOUNTS it: every
			     selection, armed confirm and filter belongs to one table and resets by construction. -->
			{#key table}
				<TableHistory
					{table}
					manifests={versions}
					{tags}
					{branches}
					producers={producerRuns}
					currentVersion={detail.describe.version ?? null}
					onchange={load}
				/>
			{/key}
		{:else if tab === 'access'}
			<section>
				<h2>Access</h2>
				<GrantsPanel dataset={table} client={grantsClient} />
				<ReadersPanel dataset={table} />
				<!-- #81 the relationship graph is heavy (SvelteFlow) — lazy-mount behind a toggle. -->
				<button class="btn ghost graphtoggle" onclick={() => (showGraph = !showGraph)}>
					{showGraph ? 'Hide' : 'Show'} authorization graph
				</button>
				{#if showGraph}<AccessGraph dataset={table} />{/if}
			</section>
		{:else}
			<section>
				<h2>Stats</h2>
				{#if partErrored(detail.stats)}
					<p class="mut">Stats unavailable right now.</p>
				{:else}
					<div class="stats mono">
						<span>{stats?.num_rows ?? '—'} rows</span>
						<span>{fmtBytes(stats?.total_bytes)}</span>
						<span>{stats?.num_indices ?? 0} indices</span>
						<!-- #78 the catalog's fixed file format (Lance columnar, storage 2.2) — never a silent guess. -->
						{#if detail.format}
							<span
								class="fmt"
								title="This catalog stores Lance only; format-selecting properties are rejected."
								>{detail.format.name} · storage v{detail.format.storage_version}</span
							>
						{/if}
						<!-- scope #6 quality gate — the validator's dataQualityAssertions verdict on the latest
					     producing run (from lineage). A plain catalog table has none, stated honestly. -->
						{#if quality}
							<span
								class="qual {quality.passed ? 'ok' : 'bad'}"
								title="Validator dataQualityAssertions on the latest producing run (lineage)."
								>quality {quality.passed ? 'passed' : 'blocked'}{quality.assertions
									? ` · ${quality.assertions} check${quality.assertions === 1 ? '' : 's'}`
									: ''}</span
							>
						{:else}
							<span class="qual none" title="No producing run has recorded dataQualityAssertions."
								>no quality gate</span
							>
						{/if}
						{#if detail.describe.location}<span class="loc">{detail.describe.location}</span>{/if}
					</div>
				{/if}
			</section>

			<section>
				<h2>Schema</h2>
				{#if schemaFields.length === 0}
					<p class="mut">Schema unavailable for this table.</p>
				{:else}
					<table>
						<thead><tr><th>field</th><th>type</th><th>nullable</th><th></th></tr></thead>
						<tbody>
							{#each schemaFields as f (f.name)}
								<tr>
									<td class="mono">{f.name}</td>
									<td class="mono">{typeName(f.type)}</td>
									<td class="mono">{f.nullable ? 'yes' : 'no'}</td>
									<td class="actions">
										{#if renaming === f.name}
											<input
												class="mono rn"
												bind:value={renameTo}
												placeholder="new name"
												aria-label="rename {f.name} to"
												onkeydown={(e) => e.key === 'Enter' && runRenameColumn()}
											/>
											<button
												class="btn ghost"
												disabled={colBusy || !renameTo.trim()}
												onclick={runRenameColumn}>save</button
											>
											<button class="btn ghost" onclick={() => (renaming = null)}>×</button>
										{:else if retyping === f.name}
											<!-- #74 tail — re-type via alter_columns; the target is a scalar Arrow type the
										     catalog's _SCALAR_ARROW map accepts. An impossible cast 400s and surfaces. -->
											<div class="retype">
												<Select
													bind:value={retypeTo}
													ariaLabel="re-type {f.name} to"
													placeholder="new type"
													options={RETYPE_TYPES.map((t) => ({ value: t, label: t }))}
												/>
												<button
													class="btn ghost"
													disabled={colBusy || !retypeTo}
													onclick={runRetypeColumn}>save</button
												>
												<button class="btn ghost" onclick={() => (retyping = null)}>×</button>
											</div>
										{:else if backfilling === f.name}
											<!-- #85 backfill — async native job over the column; the optional `where` bounds it. -->
											<input
												class="mono rn"
												bind:value={backfillWhere}
												placeholder="where (optional)"
												aria-label="backfill {f.name} where"
												onkeydown={(e) => e.key === 'Enter' && runBackfill()}
											/>
											<button class="btn ghost" disabled={colBusy} onclick={runBackfill}>run</button>
											<button class="btn ghost" onclick={() => (backfilling = null)}>×</button>
										{:else}
											<button
												class="chip-x"
												title="rename column"
												aria-label="rename {f.name}"
												disabled={colBusy}
												onclick={() => {
	renaming = f.name;
	renameTo = '';
}}>✎</button
											>
											<button
												class="chip-x"
												title="re-type column"
												aria-label="re-type {f.name}"
												disabled={colBusy}
												onclick={() => {
	retyping = f.name;
	retypeTo = '';
}}>⇄</button
											>
											<button
												class="chip-x"
												title="backfill column"
												aria-label="backfill {f.name}"
												disabled={colBusy}
												onclick={() => {
	backfilling = f.name;
	backfillWhere = '';
}}>⤵</button
											>
											<button
												class="chip-x"
												title="drop column"
												aria-label="drop {f.name}"
												disabled={colBusy}
												onclick={() => runDropColumn(f.name)}>×</button
											>
										{/if}
									</td>
								</tr>
							{/each}
						</tbody>
					</table>
				{/if}
				<!-- #74 add a SQL-expression column (e.g. price * 2, cast(null as int)). Writer-gated. -->
				<form
					class="row addcol"
					onsubmit={(e) => {
	e.preventDefault();
	runAddColumn();
}}
				>
					<input
						class="mono"
						bind:value={addColName}
						placeholder="new column"
						aria-label="New column name"
					/>
					<input
						class="mono"
						bind:value={addColExpr}
						placeholder="SQL expression (e.g. cast(null as int))"
						aria-label="Column SQL expression"
					/>
					<button
						class="btn"
						type="submit"
						disabled={colBusy || !addColName.trim() || !addColExpr.trim()}
					>
						Add column
					</button>
				</form>
				{#if colError}<p class="error">{colError}</p>{/if}
				{#if backfillMsg}<p class="mut">{backfillMsg}</p>{/if}
			</section>

			<!-- #74 tail — table + per-column property editor (writer-gated; session-only /capi BFF). -->
			<TableProperties {table} fields={schemaFields} {tableMeta} onchange={load} />

			<section>
				<h2>Insert rows</h2>
				<p class="mut">Append rows as a JSON array of objects whose keys match the schema.</p>
				<textarea
					class="mono ins"
					bind:value={insertJson}
					placeholder={'[{ "id": 1, "name": "a" }]'}></textarea>
				<div class="ins-row">
					<button class="btn" disabled={insertBusy || !insertJson.trim()} onclick={runInsert}>
						{insertBusy ? '…' : 'Insert'}
					</button>
					{#if insertMsg}<span class="ins-msg" class:okmsg={insertMsg.ok} class:error={!insertMsg.ok}
					  >{insertMsg.text}</span
					>{/if}
				</div>
			</section>

			<section>
				<h2>Update / delete rows</h2>
				<p class="mut">
					SQL predicate over the table's columns (e.g. <span class="mono">id &gt; 3</span>). Update
					applies the SET pairs to matching rows (empty predicate = all rows); delete removes them
					(predicate required). Both are writer-gated.
				</p>
				<input
					class="mono pred"
					bind:value={rowPredicate}
					placeholder="predicate (e.g. id > 3)"
					aria-label="Row predicate"
				/>
				{#each rowSets as s, i (i)}
					<div class="row setpair">
						<input
							class="mono"
							bind:value={s.column}
							placeholder="column"
							aria-label="SET column {i + 1}"
						/>
						<input
							class="mono"
							bind:value={s.expression}
							placeholder="SQL expression (e.g. price * 2)"
							aria-label="SET expression {i + 1}"
						/>
					</div>
				{/each}
				<button
					class="btn ghost"
					onclick={() => (rowSets = [...rowSets, { column: '', expression: '' }])}
				>
					+ add SET pair
				</button>
				{#if rowSetPartial}
					<p class="mut" role="status">
						A SET pair is only half-filled — complete (or clear) both its column and expression; partial
						pairs are never silently dropped.
					</p>
				{/if}
				<div class="ins-row">
					<button
						class="btn"
						disabled={rowBusy || rowSetPairs.length === 0 || rowSetPartial}
						onclick={runUpdateRows}
					>
						{rowBusy ? '…' : 'Update rows'}
					</button>
					{#if rowDeleteConfirm}
						<button
							class="btn danger"
							disabled={rowBusy || !rowPredicate.trim()}
							onclick={runDeleteRows}
						>
							confirm delete
						</button>
						<button class="btn ghost" onclick={() => (rowDeleteConfirm = false)}>cancel</button>
					{:else}
						<button
							class="btn danger"
							disabled={rowBusy || !rowPredicate.trim()}
							onclick={() => (rowDeleteConfirm = true)}
						>
							Delete rows
						</button>
					{/if}
					{#if rowMsg}<span class="ins-msg" class:okmsg={rowMsg.ok} class:error={!rowMsg.ok}
					  >{rowMsg.text}</span
					>{/if}
				</div>
			</section>

			<section>
				<h2>Blob preview</h2>
				{#if blobColumns.length === 0}
					<p class="mut">No blob columns on this table.</p>
				{:else}
					<div class="refs tagform">
						<Select
							bind:value={blobCol}
							ariaLabel="Blob column"
							placeholder="column…"
							options={blobColumns.map((c) => ({ value: c, label: c }))}
						/>
						<input class="mono" type="number" min="0" placeholder="row" bind:value={blobRow} />
						<button class="btn" disabled={!blobCol || blobRow == null} onclick={previewBlob}
							>Preview</button
						>
					</div>
					{#if blobSrc}
						{#if blobFailed}
							<p class="mut">
								Not an inline-previewable image — <a href={blobSrc}>open the blob</a>.
							</p>
						{:else}
							<img
								class="blob"
								src={blobSrc}
								alt="blob preview"
								onerror={() => (blobFailed = true)}
							/>
						{/if}
					{/if}
				{/if}
			</section>

			<section>
				<h2>Indexes</h2>
				{#if indexes.length === 0}
					<p class="mut">No indexes on this table.</p>
				{:else}
					<div class="refs">
						{#each indexes as ix (ix.index_name)}
							<span class="chip mono"
								>{ix.index_name}<span class="mut">
									· {(ix.columns ?? []).join(', ')}{ix.index_type ? ` · ${ix.index_type}` : ''}</span>
								<button
									class="chip-x"
									title="drop index"
									aria-label="drop index {ix.index_name}"
									disabled={ixBusy}
									onclick={() => runDropIndex(ix.index_name)}>×</button
								></span
							>
						{/each}
					</div>
				{/if}
				<!-- #73 build an index — scalar (BTREE/BITMAP/INVERTED/NGRAM) or vector (IVF/HNSW). Writer-gated. -->
				<form
					class="row"
					onsubmit={(e) => {
	e.preventDefault();
	runCreateIndex();
}}
				>
					<input
						class="mono"
						bind:value={ixColumn}
						placeholder="column"
						aria-label="Index column"
					/>
					<Select
						bind:value={ixType}
						ariaLabel="Index type"
						options={[
	...SCALAR_TYPES.map((t) => ({ value: t, label: `scalar · ${t}` })),
	...VECTOR_TYPES.map((t) => ({ value: t, label: `vector · ${t}` })),
]}
					/>
					{#if !ixScalar}
						<Select
							bind:value={ixDistance}
							ariaLabel="Distance type"
							options={[
	{ value: 'cosine', label: 'cosine' },
	{ value: 'l2', label: 'l2' },
	{ value: 'dot', label: 'dot' },
]}
						/>
					{/if}
					<button class="btn" type="submit" disabled={ixBusy || !ixColumn.trim()}>
						{ixBusy ? '…' : 'Build index'}
					</button>
				</form>
				{#if ixError}<p class="error">{ixError}</p>{/if}
			</section>

			<section>
				<h2>Maintenance policy</h2>
				{#if editingPolicy}
					<div class="policy-edit">
						<label
							>retention days <input
								class="mono"
								type="number"
								min="1"
								bind:value={draft.retention_days}
								placeholder="global default"
							/></label
						>
						<label
							>retain versions <input
								class="mono"
								type="number"
								min="1"
								bind:value={draft.retain_versions}
								placeholder="—"
							/></label
						>
						<label
							>compact every (h) <input
								class="mono"
								type="number"
								min="1"
								bind:value={draft.interval}
								placeholder="every sweep"
							/></label
						>
						<label
							>target rows/fragment <input
								class="mono"
								type="number"
								min="1024"
								bind:value={draft.target}
								placeholder="Lance default"
							/></label
						>
						<label class="check"
							><input type="checkbox" bind:checked={draft.enabled} /> maintenance enabled</label
						>
						<div class="row">
							<button class="btn" disabled={busy} onclick={savePolicy}>Save policy</button>
							<button class="btn ghost" onclick={() => (editingPolicy = false)}>Cancel</button>
						</div>
					</div>
				{:else if policyUnavailable}
					<p class="mut">
						Policy unavailable right now — not shown to avoid an overwriting edit against a stale read.
					</p>
				{:else if policy}
					<div class="refs">
						{#if policy.retention_days}<span class="chip mono">retention {policy.retention_days}d</span>{/if}
						{#if policy.retain_versions}<span class="chip mono">keep last {policy.retain_versions}</span>{/if}
						{#if policy.compact_interval_hours}<span class="chip mono">every {policy.compact_interval_hours}h</span>{/if}
						{#if policy.target_rows_per_fragment}<span class="chip mono">target {policy.target_rows_per_fragment}
						rows/frag</span>{/if}
						{#if !policy.compact_enabled}<span class="chip off mono">maintenance off</span>{/if}
						<button class="btn ghost" onclick={startPolicyEdit}>Edit</button>
						<button class="btn ghost danger" disabled={busy} onclick={removePolicy}>
							<Trash2 size={12} /> Remove
						</button>
					</div>
					<p class="mut">
						Enforced by the compaction sweep; tag-pinned versions (e.g. blessed) are never cleaned up.
					</p>
				{:else}
					<p class="mut">
						No policy — the sweep applies the global defaults.
						<button class="btn ghost" onclick={startPolicyEdit}>Set policy</button>
					</p>
				{/if}
				{#if policyError}<p class="error">{policyError}</p>{/if}

				<!-- #75 on-demand GC: dry-run reclaimable versions, then reclaim (owner-gated, destructive). -->
				<div class="gc">
					<h3>Garbage collection</h3>
					<div class="row">
						<label
							>older than (days) <input
								class="mono"
								type="number"
								min="1"
								bind:value={gcDays}
								placeholder="any age"
							/></label
						>
						<label
							>keep last <input
								class="mono"
								type="number"
								min="1"
								bind:value={gcKeep}
								placeholder="—"
							/></label
						>
						<button class="btn ghost" disabled={gcBusy || !gcHasBound} onclick={runGcPreview}>
							Preview
						</button>
					</div>
					{#if gcPreview}
						<p class="mut">
							{gcPreview.eligible_versions.length} version{gcPreview.eligible_versions.length === 1
				? ''
				: 's'}
							reclaimable
							{#if gcPreview.eligible_versions.length}(v{gcPreview.eligible_versions.join(', v')}){/if}
							· {gcPreview.total_versions} total, current v{gcPreview.current_version}.
							{#if Object.keys(gcPreview.protected_tags).length}
								Protected by tags: {Object.entries(gcPreview.protected_tags)
									.map(([t, v]) => `${t}→v${v}`)
									.join(', ')}.
							{/if}
						</p>
						{#if gcPreview.eligible_versions.length}
							{#if gcConfirm}
								<div class="row">
									<span class="mut"
										>Permanently reclaim {gcPreview.eligible_versions.length} version(s)?</span
									>
									<button class="btn danger" disabled={gcBusy} onclick={runGc}>Confirm reclaim</button>
									<button class="btn ghost" onclick={() => (gcConfirm = false)}>Cancel</button>
								</div>
							{:else}
								<button class="btn" disabled={gcBusy} onclick={() => (gcConfirm = true)}>
									<Trash2 size={12} /> Reclaim now
								</button>
							{/if}
						{/if}
					{/if}
					{#if gcResult}<p class="mut">{gcResult}</p>{/if}

					<!-- #76 compact-now: merge small fragments (non-destructive), using the policy's target size. -->
					<div class="row gc-compact">
						<button class="btn ghost" disabled={compactBusy} onclick={runCompact}>
							{compactBusy ? 'compacting…' : 'Compact now'}
						</button>
						{#if compactResult}<span class="mut">{compactResult}</span>{/if}
					</div>
					{#if gcError}<p class="error">{gcError}</p>{/if}
				</div>
			</section>

			<!-- #85 danger zone — rename navigates to the new id; drop/deregister confirm via AlertDialog. -->
			<section class="dangerzone">
				<h2>Danger zone</h2>
				<form
					class="row"
					onsubmit={(e) => {
	e.preventDefault();
	runRenameTable();
}}
				>
					<input
						class="mono"
						bind:value={renameTableTo}
						placeholder="new table name"
						aria-label="Rename table to"
					/>
					<button class="btn" type="submit" disabled={dangerBusy || !renameTableTo.trim()}>
						Rename
					</button>
				</form>
				<p class="mut">
					Rename relocates the table within its namespace and navigates to the new id (owner-gated:
					can_drop on the source + can_create_table on the destination).
				</p>
				<div class="row">
					<button class="btn danger" disabled={dangerBusy} onclick={() => openDanger('deregister')}>
						Deregister
					</button>
					<button class="btn danger" disabled={dangerBusy} onclick={() => openDanger('drop')}>
						<Trash2 size={12} /> Drop table
					</button>
				</div>
				<p class="mut">
					Deregister detaches the table from the catalog (data stays on storage); drop deletes it
					permanently.
				</p>
				{#if dangerError}<p class="error">{dangerError}</p>{/if}
			</section>
		{/if}
	{/if}
</div>

<AlertDialog.Root bind:open={dangerOpen}>
	<AlertDialog.Content>
		<AlertDialog.Title>
			{dangerAction === 'deregister' ? 'Deregister' : 'Drop'} table {table}
		</AlertDialog.Title>
		<AlertDialog.Description>
			{#if dangerAction === 'deregister'}
				This detaches <span class="mono">{table}</span> from the catalog (owner-gated: can_deregister). The
				data stays on storage, but the catalog forgets the id and its grants are revoked.
			{:else}
				This permanently drops <span class="mono">{table}</span> and its data (owner-gated: can_drop). Every
				version, tag and branch is deleted; its grants are revoked.
			{/if}
		</AlertDialog.Description>
		<div class="dialog-actions">
			<AlertDialog.Cancel disabled={dangerBusy}>Cancel</AlertDialog.Cancel>
			<AlertDialog.Action
				class="border-destructive/40 bg-destructive/15 text-destructive hover:bg-destructive/25"
				disabled={dangerBusy}
				onclick={confirmDanger}
			>
				{dangerAction === 'deregister' ? 'Deregister' : 'Drop'}
			</AlertDialog.Action>
		</div>
	</AlertDialog.Content>
</AlertDialog.Root>

<style>
	.recover {
		border: 1px solid color-mix(in srgb, var(--warn) 45%, var(--line));
		background: color-mix(in srgb, var(--warn) 7%, transparent);
		border-radius: var(--radius-sm);
		padding: 16px 18px;
		margin: 24px 0;
	}
	.recover h2 {
		margin: 0 0 8px;
		font-size: 15px;
	}
	.recover p {
		margin: 0 0 8px;
		font-size: 13px;
	}
	.recover .problem {
		color: var(--fail);
	}

	.page {
		max-width: 860px;
		margin: 0 auto;
		padding: 56px 20px 40px;
	}
	header {
		display: flex;
		align-items: baseline;
		gap: 10px;
		margin-bottom: 18px;
	}
	h1 {
		font-size: 18px;
		margin: 0;
	}
	h2 {
		font-size: 13px;
		text-transform: uppercase;
		letter-spacing: 0.06em;
		color: var(--faint);
		margin: 0 0 8px;
	}
	section {
		margin-bottom: 22px;
	}
	.sub {
		color: var(--faint);
		font-size: 12px;
	}
	.stats {
		display: flex;
		flex-wrap: wrap;
		gap: 14px;
		font-size: 12px;
		color: var(--mut);
	}
	.loc {
		color: var(--faint);
	}
	.fmt {
		border: 1px solid color-mix(in srgb, var(--accent, #ffc14d) 45%, var(--line));
		border-radius: var(--radius-sm);
		padding: 0 6px;
		color: var(--mut);
	}
	.qual {
		border-radius: var(--radius-sm);
		padding: 0 6px;
		border: 1px solid var(--line);
	}
	.qual.ok {
		color: var(--ok);
		border-color: color-mix(in srgb, var(--ok) 45%, var(--line));
	}
	.qual.bad {
		color: var(--fail);
		border-color: color-mix(in srgb, var(--fail) 45%, var(--line));
	}
	.qual.none {
		color: var(--faint);
	}
	table {
		border-collapse: collapse;
		font-size: 12px;
		width: 100%;
	}
	th {
		text-align: left;
		color: var(--faint);
		font-weight: 500;
		padding: 3px 14px 3px 0;
		border-bottom: 1px solid var(--line);
	}
	td {
		padding: 3px 14px 3px 0;
		border-bottom: 1px solid color-mix(in srgb, var(--line) 45%, transparent);
	}
	.refs {
		display: flex;
		flex-wrap: wrap;
		align-items: center;
		gap: 6px;
		margin-bottom: 6px;
		font-size: 12px;
	}
	.chip {
		background: var(--panel-2);
		border: 1px solid var(--line);
		border-radius: var(--radius-sm);
		padding: 0 7px;
	}
	.chip-x {
		margin-left: 5px;
		background: none;
		border: none;
		padding: 0;
		color: var(--faint);
		font: inherit;
		cursor: pointer;
	}
	td.actions {
		text-align: right;
		white-space: nowrap;
	}
	.rn {
		width: 110px;
		background: var(--panel-2);
		border: 1px solid var(--line);
		border-radius: var(--radius-sm);
		color: var(--ink);
		font-size: 12px;
		padding: 2px 6px;
	}
	.addcol {
		margin-top: 10px;
	}
	.chip-x:hover {
		color: var(--fail);
	}
	.chip-x:disabled {
		opacity: 0.4;
		cursor: default;
	}
	.chip.off {
		border-color: color-mix(in srgb, var(--amber) 55%, var(--line));
	}
	.mut {
		color: var(--faint);
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
		padding: 32px 0;
	}
	.policy-edit {
		display: flex;
		flex-wrap: wrap;
		gap: 12px;
		align-items: end;
		font-size: 12px;
		color: var(--mut);
	}
	.policy-edit label {
		display: flex;
		flex-direction: column;
		gap: 3px;
	}
	.policy-edit label.check {
		flex-direction: row;
		align-items: center;
		gap: 6px;
	}
	.policy-edit input[type='number'],
	.gc input[type='number'] {
		width: 110px;
		background: var(--panel-2);
		border: 1px solid var(--line);
		border-radius: var(--radius-sm);
		color: var(--ink);
		padding: 4px 8px;
		font-size: 12px;
	}
	.gc {
		margin-top: 14px;
		padding-top: 12px;
		border-top: 1px solid color-mix(in srgb, var(--line) 45%, transparent);
	}
	.gc h3 {
		font-size: 13px;
		margin: 0 0 8px;
	}
	.gc label {
		display: inline-flex;
		flex-direction: column;
		gap: 3px;
		font-size: 12px;
		color: var(--mut);
	}
	.gc-compact {
		align-items: center;
		margin-top: 8px;
	}
	.tagform input,
	.tagform input {
		width: 150px;
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
	.btn:disabled {
		opacity: 0.5;
		cursor: default;
	}
	.blob {
		display: block;
		max-width: 100%;
		max-height: 320px;
		border: 1px solid var(--line);
		border-radius: var(--radius-sm);
		margin-top: 10px;
	}
	.ins {
		width: 100%;
		min-height: 72px;
		background: var(--panel-2);
		border: 1px solid var(--line);
		border-radius: var(--radius-sm);
		color: var(--ink);
		font-size: 12px;
		padding: 7px 9px;
		resize: vertical;
	}
	.ins-row {
		display: flex;
		align-items: center;
		gap: 10px;
		margin-top: 6px;
	}
	.ins-msg {
		font-size: 12px;
	}
	.okmsg {
		color: var(--ok);
	}
	.btn.ghost {
		background: none;
		color: var(--mut);
	}
	.graphtoggle {
		margin: 10px 0 8px;
	}
	.btn.danger {
		color: var(--fail);
	}
	.row {
		display: flex;
		gap: 8px;
	}
	.pred {
		width: 100%;
		margin-bottom: 6px;
	}
	.setpair {
		margin-bottom: 6px;
	}
	.dangerzone {
		border-top: 1px solid color-mix(in srgb, var(--fail) 30%, var(--line));
		padding-top: 12px;
	}
	.dangerzone form {
		margin-bottom: 4px;
	}
	.dangerzone .row {
		margin: 8px 0 4px;
	}
	.dialog-actions {
		display: flex;
		justify-content: flex-end;
		gap: 8px;
	}
</style>
