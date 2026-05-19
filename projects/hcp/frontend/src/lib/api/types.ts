export interface TokenResponse {
  access_token: string;
  token_type: string;
}

export interface HealthResponse {
  status: string;
}

export interface BucketInfo {
  name: string;
  creation_date: string;
}

export interface ListBucketsResponse {
  buckets: BucketInfo[];
  owner: string;
}

export interface ObjectInfo {
  key: string;
  last_modified: string;
  size: number;
  etag: string;
  storage_class?: string;
}

export interface ListObjectsResponse {
  objects: ObjectInfo[];
  prefix: string;
  is_truncated: boolean;
  next_continuation_token?: string;
  key_count: number;
}

export interface Tenant {
  name: string;
  systemVisibleDescription?: string;
  hardQuota?: string;
  softQuota?: string;
  namespaceCount?: number;
  tags?: Record<string, string>;
}

export interface Namespace {
  name: string;
  description?: string;
  hardQuota?: string;
  softQuota?: string;
  optimizedFor?: string;
  hashScheme?: string;
  tags?: Record<string, string>;
}

export interface UserAccount {
  username: string;
  fullName?: string;
  description?: string;
  enabled?: boolean;
  localAuthentication?: boolean;
  roles?: string[];
}

export interface ApiError {
  detail: string;
}
