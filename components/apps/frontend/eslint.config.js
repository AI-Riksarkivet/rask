import js from '@eslint/js';
import ts from 'typescript-eslint';
import svelte from 'eslint-plugin-svelte';
import globals from 'globals';

export default ts.config(
  js.configs.recommended,
  ...ts.configs.recommended,
  ...svelte.configs['flat/recommended'],
  {
    languageOptions: {
      globals: {
        ...globals.browser,
        ...globals.node,
      },
    },
  },
  {
    files: ['**/*.svelte', '**/*.svelte.ts'],
    languageOptions: {
      parserOptions: {
        parser: ts.parser,
      },
    },
    rules: {
      // $effect() blocks are expressions, not assignments — this is expected Svelte 5 usage
      '@typescript-eslint/no-unused-expressions': 'off',
    },
  },
  {
    rules: {
      // Allow `any` as warning — too pervasive in ONNX worker code to enforce now
      '@typescript-eslint/no-explicit-any': 'warn',
      // Ignore unused vars that start with _ or are type-only imports
      '@typescript-eslint/no-unused-vars': [
        'error',
        {
          argsIgnorePattern: '^_',
          varsIgnorePattern: '^_',
          caughtErrorsIgnorePattern: '^_',
        },
      ],
      // These are style preferences that are too noisy for this codebase right now.
      // Map/Set/URL in non-reactive contexts (workers, callbacks) are fine.
      'svelte/prefer-svelte-reactivity': 'off',
      // {#each} key expressions are good practice but not required
      'svelte/require-each-key': 'warn',
      // goto()/href without resolve() — not using shallow routing
      'svelte/no-navigation-without-resolve': 'off',
    },
  },
  {
    ignores: [
      '.svelte-kit/',
      'build/',
      'node_modules/',
      '.storybook/',
      'storybook-static/',
    ],
  },
);
