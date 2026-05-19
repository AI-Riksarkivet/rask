// See https://svelte.dev/docs/kit/types#app.d.ts

declare global {
  const __APP_VERSION__: string;

  namespace App {
    // interface Error {}
    interface Locals {
      token?: string;
      sessions: import("$lib/types/session.js").TenantSession[];
    }
    // interface PageData {}
    // interface PageState {}
    // interface Platform {}
  }
}

export {};
