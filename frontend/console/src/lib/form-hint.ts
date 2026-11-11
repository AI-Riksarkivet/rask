/** Why a create form's submit is disabled: the required fields still empty, named, or `null` when
 *  every one is filled. A disabled button that does not say why reads as broken. */
export function missingFieldsHint(
	action: string,
	fields: readonly (readonly [label: string, value: string])[],
): string | null {
	const missing = fields.filter(([, value]) => value.trim() === '').map(([label]) => label);
	if (missing.length === 0) return null;
	const list =
		missing.length === 1
			? missing[0]
			: `${missing.slice(0, -1).join(', ')} and ${missing.at(-1) ?? ''}`;
	return `${action} needs ${list}.`;
}
