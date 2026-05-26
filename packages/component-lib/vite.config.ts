import { svelte } from '@sveltejs/vite-plugin-svelte';
import tailwindcss from '@tailwindcss/vite';
import { defineConfig } from 'vite';

// Used by Storybook to render components
export default defineConfig({
	plugins: [tailwindcss(), svelte()],
});
