<script lang="ts">
	import NamespacePermissionsEditor from '$lib/components/custom/namespace-permissions-editor/namespace-permissions-editor.svelte';
	import {
		get_user_permissions,
		set_user_permissions,
		type DataAccessPermissions,
	} from '$lib/remote/users.remote.js';

	let {
		tenant,
		username,
	}: {
		tenant: string;
		username: string;
	} = $props();

	let permsData = $derived(get_user_permissions({ tenant, username }));
</script>

<NamespacePermissionsEditor
	{tenant}
	label="user"
	{permsData}
	onsave={async (body) => {
		if (!permsData) return;
		await set_user_permissions({
			tenant,
			username,
			body: body as unknown as Record<string, unknown>,
		}).updates(permsData);
	}}
/>
