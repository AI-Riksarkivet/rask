// #lib/api — the console's API client, split by domain.
//   • parse  — the valibot parse-don't-validate boundary for typed client responses.
//   • me     — the frozen `GET /management/v1/me` identity contract (schema/types) and its fetch.
//   • client — the browser transport (`client.ts`): same-origin `/api/*` with the person's bearer.
export * from './parse';
export * from './me';
