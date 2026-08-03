// Wire CONTRACTS for the MEDALLION (lance-ray) trigger door. The transport lives in
// `remote/medallion.remote.ts` (the zone's remote-function dialect); lance-ray stays its own
// backend (the cascade + train head), distinct from the catalog and lineage planes.

/** The /produce 202 body — a correlation `token` the cascade's run_ids derive from (or a 503 problem+json,
 * surfaced as ApiResult.status). */
export type ProduceResult = { status?: string; token?: string };
/** The /train 202 body — the correlation token + the model it will bless (or 409/422/503). */
export type TrainResult = { status?: string; token?: string; model?: string };

export type TrainFeature = { dataset: string; version?: number };
