<script lang="ts">
	import CorsEditor from '$lib/components/custom/cors-editor/cors-editor.svelte';
	import {
		get_ns_cors,
		set_ns_cors,
		delete_ns_cors,
		type CorsConfig,
	} from '$lib/remote/namespaces.remote.js';

	let {
		tenant,
		namespaceName,
	}: {
		tenant: string;
		namespaceName: string;
	} = $props();

	let corsData = $derived(get_ns_cors({ tenant, name: namespaceName }));
	let cors = $derived((corsData?.current ?? {}) as CorsConfig);
	let loading = $derived(corsData?.loading ?? false);

	async function handleSave(xml: string) {
		if (!corsData) return;
		await set_ns_cors({ tenant, name: namespaceName, body: { cors: xml } }).updates(corsData);
	}

	async function handleDelete() {
		if (!corsData) return;
		await delete_ns_cors({ tenant, name: namespaceName }).updates(corsData);
	}
</script>

<CorsEditor
	corsXml={cors.cors ?? ''}
	{loading}
	description="Cross-Origin Resource Sharing rules controlling which external origins can access this namespace. For presigned multipart uploads, ensure PUT is in AllowedMethods and ETag is in ExposeHeaders. Overrides the tenant-level CORS configuration."
	onsave={handleSave}
	ondelete={handleDelete}
/>
