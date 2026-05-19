<script lang="ts">
	import NamespacePermissionsEditor from '$lib/components/custom/namespace-permissions-editor/namespace-permissions-editor.svelte';
	import {
		get_group_permissions,
		set_group_permissions,
		type DataAccessPermissions,
	} from '$lib/remote/users.remote.js';

	let {
		tenant,
		groupname,
	}: {
		tenant: string;
		groupname: string;
	} = $props();

	let permsData = $derived(get_group_permissions({ tenant, groupname }));
</script>

<NamespacePermissionsEditor
	{tenant}
	label="group"
	{permsData}
	onsave={async (body) => {
		if (!permsData) return;
		await set_group_permissions({
			tenant,
			groupname,
			body: body as unknown as Record<string, unknown>,
		}).updates(permsData);
	}}
/>
