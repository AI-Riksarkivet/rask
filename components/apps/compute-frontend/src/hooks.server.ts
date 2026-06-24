import type { HandleFetch } from '@sveltejs/kit';
import { rewriteApiToGateway } from '@rask/api';
import { GATEWAY_URL } from '$lib/server/env';

export const handleFetch: HandleFetch = ({ request, fetch }) =>
	fetch(rewriteApiToGateway(request, GATEWAY_URL));
