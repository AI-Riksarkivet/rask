<script lang="ts">
	import { base } from '$app/paths';
	import { page } from '$app/state';
	import { AppError } from '@rask/ui/shell';
</script>

<!-- The same branded degrade as every other zone (resilient-page principle): a failed route here must
     not render a blank document or SvelteKit's default page. `home` points at this zone's own root so
     the recovery link stays inside the zone the user was already in. -->
<AppError status={page.status} message={page.error?.message} home={base || '/'} />
