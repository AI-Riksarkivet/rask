import path from 'node:path';
import { includeIgnoreFile } from '@eslint/compat';
import prettier from 'eslint-config-prettier';
import svelte from 'eslint-plugin-svelte';
import { defineConfig } from 'eslint/config';
import globals from 'globals';
import ts from 'typescript-eslint';
import computeSvelteConfig from './components/apps/compute-frontend/svelte.config.js';
import discoverSvelteConfig from './components/apps/discover-frontend/svelte.config.js';
import frontendSvelteConfig from './components/apps/frontend/svelte.config.js';
import overviewSvelteConfig from './components/apps/overview-frontend/svelte.config.js';
import storageSvelteConfig from './components/apps/storage-frontend/svelte.config.js';
import studioSvelteConfig from './components/apps/studio-frontend/svelte.config.js';
import trainSvelteConfig from './components/apps/train-frontend/svelte.config.js';
import libSvelteConfig from './packages/ui/svelte.config.js';

const gitignorePath = path.resolve(import.meta.dirname, '.gitignore');

// --- Local rule: cross-zone <a> links must hard-navigate ---------------------
// In the microfrontend split each zone (`/default/<domain>`) is a SEPARATE
// SvelteKit app. A soft client-router nav to another zone's path targets a route
// THIS app doesn't own → 404. Cross-zone <a>s must therefore set
// `data-sveltekit-reload` to force a full document navigation. The shared shell
// (`@rask/ui/shell` nav-main) does this dynamically via `crossZone(href)`; this
// rule guards the hand-written links in app pages so the convention can't silently
// drift back. Same-zone links use `{base}/…` (a `{base}` expression, never a literal
// `/default/<domain>`), so they read as an opaque placeholder here and are ignored.
const ZONE_PATH = /^\/[^/]*\/(overview|compute|discover|storage|train|studio)(?:\/|$)/;
const EXPR = '￿'; // opaque-expression placeholder (can't contain a '/')

// Reconstruct a comparable path string from an href attribute's value nodes.
// Static text is kept verbatim; every `${…}`/`{…}` becomes EXPR. Returns null for
// shorthand/spread hrefs we can't read statically (never flagged).
function hrefToPath(valueNodes) {
	if (!valueNodes || valueNodes.length === 0) return null;
	let out = '';
	for (const n of valueNodes) {
		if (n.type === 'SvelteLiteral') {
			out += n.value;
		} else if (n.type === 'SvelteMustacheTag') {
			const e = n.expression;
			if (e && e.type === 'TemplateLiteral') {
				out += e.quasis.map((q) => q.value.cooked ?? q.value.raw).join(EXPR);
			} else {
				out += EXPR;
			}
		} else {
			return null;
		}
	}
	return out;
}

// `data-sveltekit-reload` present and not explicitly "off".
function hasReloadEnabled(attrs) {
	for (const a of attrs) {
		if (a.type === 'SvelteAttribute' && a.key?.name === 'data-sveltekit-reload') {
			const v = a.value;
			if (v?.length === 1 && v[0].type === 'SvelteLiteral' && v[0].value === 'off') return false;
			return true; // boolean shorthand, "", or a dynamic value → enabled
		}
	}
	return false;
}

const raLocal = {
	rules: {
		'cross-zone-reload': {
			meta: {
				type: 'problem',
				docs: {
					description:
						'Cross-zone <a> links (into another microfrontend /default/<domain>) must set data-sveltekit-reload.',
				},
				messages: {
					missingReload:
						'Cross-zone link to "{{href}}" must set data-sveltekit-reload — a soft client nav targets a route this microfrontend zone does not own (→ 404). Add data-sveltekit-reload, or use {base}/… for a same-zone link.',
				},
				schema: [],
			},
			create(context) {
				return {
					SvelteElement(node) {
						const nm = node.name;
						const tag = typeof nm === 'string' ? nm : nm?.name;
						if (tag !== 'a') return;
						const attrs = node.startTag?.attributes ?? [];
						const hrefAttr = attrs.find(
							(a) => a.type === 'SvelteAttribute' && a.key?.name === 'href',
						);
						if (!hrefAttr) return;
						const path = hrefToPath(hrefAttr.value);
						if (!path || !ZONE_PATH.test(path)) return;
						if (hasReloadEnabled(attrs)) return;
						context.report({
							node,
							messageId: 'missingReload',
							data: { href: path.replaceAll(EXPR, '${…}') },
						});
					},
				};
			},
		},
	},
};

export default defineConfig(
	includeIgnoreFile(gitignorePath),
	ts.configs.recommended,
	svelte.configs.recommended,
	prettier,
	svelte.configs.prettier,
	{
		languageOptions: {
			globals: { ...globals.browser, ...globals.node },
		},
		rules: {
			// typescript-eslint owns this; the lint rule throws false positives in TS projects.
			// https://typescript-eslint.io/troubleshooting/faqs/eslint/#i-get-errors-from-the-no-undef-rule
			'no-undef': 'off',
			'@typescript-eslint/no-explicit-any': 'warn',
			'@typescript-eslint/no-unused-vars': [
				'error',
				{
					argsIgnorePattern: '^_',
					varsIgnorePattern: '^_',
					caughtErrorsIgnorePattern: '^_',
				},
			],
			// Map/Set/URL in non-reactive contexts (workers, callbacks) are fine.
			'svelte/prefer-svelte-reactivity': 'off',
			// GATE: keyless {#each} silently re-renders/reorders DOM on mutation — a
			// class of bug recent audits caught by hand. Error, not warn, so CI fails.
			'svelte/require-each-key': 'error',
			// GATE: bind reactive state, never reassign it imperatively — catches the
			// "assigned but not reactive" footgun. Zero violations on current code.
			'svelte/no-reactive-reassign': 'error',
			// Not using shallow routing.
			'svelte/no-navigation-without-resolve': 'off',
		},
	},
	{
		files: ['**/*.svelte', '**/*.svelte.ts', '**/*.svelte.js'],
		rules: {
			// $effect() blocks are expressions, not assignments — expected Svelte 5 usage.
			'@typescript-eslint/no-unused-expressions': 'off',
		},
	},
	{
		files: ['components/apps/frontend/**/*.svelte', 'components/apps/frontend/**/*.svelte.ts'],
		languageOptions: {
			parserOptions: {
				projectService: true,
				extraFileExtensions: ['.svelte'],
				parser: ts.parser,
				svelteConfig: frontendSvelteConfig,
			},
		},
	},
	{
		files: [
			'components/apps/storage-frontend/**/*.svelte',
			'components/apps/storage-frontend/**/*.svelte.ts',
		],
		languageOptions: {
			parserOptions: {
				projectService: true,
				extraFileExtensions: ['.svelte'],
				parser: ts.parser,
				svelteConfig: storageSvelteConfig,
			},
		},
	},
	{
		files: [
			'components/apps/compute-frontend/**/*.svelte',
			'components/apps/compute-frontend/**/*.svelte.ts',
		],
		languageOptions: {
			parserOptions: {
				projectService: true,
				extraFileExtensions: ['.svelte'],
				parser: ts.parser,
				svelteConfig: computeSvelteConfig,
			},
		},
	},
	{
		files: [
			'components/apps/discover-frontend/**/*.svelte',
			'components/apps/discover-frontend/**/*.svelte.ts',
		],
		languageOptions: {
			parserOptions: {
				projectService: true,
				extraFileExtensions: ['.svelte'],
				parser: ts.parser,
				svelteConfig: discoverSvelteConfig,
			},
		},
	},
	{
		files: [
			'components/apps/overview-frontend/**/*.svelte',
			'components/apps/overview-frontend/**/*.svelte.ts',
		],
		languageOptions: {
			parserOptions: {
				projectService: true,
				extraFileExtensions: ['.svelte'],
				parser: ts.parser,
				svelteConfig: overviewSvelteConfig,
			},
		},
	},
	{
		files: [
			'components/apps/studio-frontend/**/*.svelte',
			'components/apps/studio-frontend/**/*.svelte.ts',
		],
		languageOptions: {
			parserOptions: {
				projectService: true,
				extraFileExtensions: ['.svelte'],
				parser: ts.parser,
				svelteConfig: studioSvelteConfig,
			},
		},
	},
	{
		files: [
			'components/apps/train-frontend/**/*.svelte',
			'components/apps/train-frontend/**/*.svelte.ts',
		],
		languageOptions: {
			parserOptions: {
				projectService: true,
				extraFileExtensions: ['.svelte'],
				parser: ts.parser,
				svelteConfig: trainSvelteConfig,
			},
		},
	},
	{
		files: ['packages/ui/**/*.svelte', 'packages/ui/**/*.svelte.ts'],
		languageOptions: {
			parserOptions: {
				projectService: true,
				extraFileExtensions: ['.svelte'],
				parser: ts.parser,
				svelteConfig: libSvelteConfig,
			},
		},
	},
	{
		// GATE: cross-zone <a> links must hard-navigate (see ZONE_PATH rule above).
		files: ['components/apps/**/*.svelte'],
		plugins: { 'ra-local': raLocal },
		rules: { 'ra-local/cross-zone-reload': 'error' },
	},
	{
		ignores: [
			'**/.svelte-kit/',
			'**/build/',
			'**/dist/',
			'**/storybook-static/',
			'packages/ui/.storybook/',
		],
	},
);
