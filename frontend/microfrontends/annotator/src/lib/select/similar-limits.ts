/** The service's own bound on one "more like this" call (`similar.MAX_N`).
 *
 *  Its own tiny module so both the wire schema and the UI quote the same number without the schema
 *  importing the UI or vice versa. Named rather than inlined for the reason the SEND cap taught:
 *  a client-side bound that disagrees with the server is not a bound, it is a delay before a
 *  refusal. */
export const MAX_SIMILAR_N = 200;
