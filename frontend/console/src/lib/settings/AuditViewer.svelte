<script lang="ts">
	// `/settings/audit` — where the compliance trail lives. Every lakehouse service writes it to the
	// `lance.audit` log, which OpenTelemetry carries to the platform's telemetry store; no lakehouse
	// door serves it and the console has no backend to query that store. So the page says where the
	// trail is and offers no control that could not act. Kept by the owner's decision (XC-192). An
	// ESTATE surface: the trail spans every project.
	import { ScrollText } from '@lucide/svelte';
</script>

<div class="page">
	<header>
		<ScrollText size={16} />
		<h1>Audit</h1>
		<span class="sub">who did what, and how it was decided</span>
	</header>
	<p class="empty">
		The audit trail lives in the platform's telemetry store (the <code>lance.audit</code> log), which
		this console cannot query: read it in the platform's own log tools.
	</p>
</div>

<style>
	.page {
		max-width: 980px;
		margin: 0 auto;
		padding: 56px 20px 40px;
	}
	header {
		display: flex;
		align-items: baseline;
		gap: 10px;
		margin-bottom: 16px;
	}
	h1 {
		font-size: 20px;
		margin: 0;
	}
	.sub {
		color: var(--muted-foreground);
		font-size: 12px;
	}
	.empty {
		color: var(--muted-foreground);
		font-size: 13px;
		padding: 30px 0;
		max-width: 72ch;
	}
</style>
