<script lang="ts">
	import type { RemoteQuery } from '@sveltejs/kit';
	import type { Namespace } from '$lib/remote/namespaces.remote.js';
	import NsTemplateExport from './ns-template-export.svelte';
	import NsTemplateImport from './ns-template-import.svelte';

	let {
		tenant,
		nsData,
	}: {
		tenant: string;
		nsData: RemoteQuery<Namespace[]>;
	} = $props();

	let namespaces = $derived((nsData?.current ?? []) as Namespace[]);
</script>

<div class="grid gap-6 lg:grid-cols-2">
	<NsTemplateExport {tenant} {namespaces} />
	<NsTemplateImport {tenant} {namespaces} {nsData} />
</div>
