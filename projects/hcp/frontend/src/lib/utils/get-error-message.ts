import { isHttpError } from "@sveltejs/kit";

/**
 * Extract a user-facing error message from an unknown caught value.
 * Handles both standard Error instances and SvelteKit HttpError objects.
 */
export function getErrorMessage(err: unknown, fallback: string): string {
  if (isHttpError(err)) return err.body.message;
  if (err instanceof Error) return err.message;
  return fallback;
}
