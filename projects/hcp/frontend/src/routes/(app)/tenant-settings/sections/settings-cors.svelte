<script lang="ts">
	import CorsEditor from '$lib/components/custom/cors-editor/cors-editor.svelte';
	import CardSkeleton from '$lib/components/ui/skeleton/card-skeleton.svelte';
	import {
		get_tenant_cors,
		set_tenant_cors,
		delete_tenant_cors,
		type TenantCors,
	} from '$lib/remote/tenant-info.remote.js';

	let {
		tenant,
	}: {
		tenant: string;
	} = $props();

	let corsData = $derived(get_tenant_cors({ tenant }));
	let cors = $derived((corsData?.current ?? {}) as TenantCors);
	let loading = $derived(corsData?.loading ?? false);

	async function handleSave(xml: string) {
		if (!corsData) return;
		await set_tenant_cors({ tenant, body: { cors: xml } }).updates(corsData);
	}

	async function handleDelete() {
		if (!corsData) return;
		await delete_tenant_cors({ tenant }).updates(corsData);
	}
</script>

{#snippet skeletonSnippet()}
	<CardSkeleton />
{/snippet}

<CorsEditor
	corsXml={cors.cors ?? ''}
	{loading}
	description="Tenant-wide Cross-Origin Resource Sharing rules for browser-based S3 access. For presigned multipart uploads, ensure PUT is in AllowedMethods and ETag is in ExposeHeaders. Namespace-level CORS overrides these defaults."
	onsave={handleSave}
	ondelete={handleDelete}
	skeleton={skeletonSnippet}
/>
