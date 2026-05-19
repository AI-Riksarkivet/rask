export interface LanceField {
  name: string;
  type: string;
  nullable: boolean;
  is_vector: boolean;
  is_binary: boolean;
  vector_dim: number | null;
}

export interface VectorValue {
  type: "vector";
  dim: number;
  norm: number;
  min: number;
  max: number;
  mean: number;
  preview: number[];
}

export interface BinaryCellMeta {
  size: number;
}

export type CellValue =
  | string
  | number
  | boolean
  | null
  | VectorValue
  | BinaryCellMeta;

export interface VectorPreviewEntry {
  norm: number;
  sample: number[];
}

export interface VectorPreviewStats {
  count: number;
  dim: number;
  min: number;
  max: number;
  mean: number;
}

/**
 * Build a URL for the /cell endpoint that streams raw binary content.
 * Used as <img src={cellUrl(...)}> for image columns.
 */
export function cellUrl(
  bucket: string,
  table: string,
  column: string,
  row: number,
  path?: string,
): string {
  const params = new URLSearchParams({
    bucket,
    table,
    column,
    row: String(row),
  });
  if (path) params.set("path", path);
  return `/api/v1/lance/cell?${params}`;
}
