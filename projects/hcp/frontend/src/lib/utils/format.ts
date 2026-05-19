const BYTE_UNITS = ["B", "KB", "MB", "GB", "TB", "PB"] as const;

export function formatBytes(bytes: number, decimals = 1): string {
  if (bytes === 0) return "0 B";
  const k = 1024;
  const i = Math.floor(Math.log(bytes) / Math.log(k));
  const value = bytes / Math.pow(k, i);
  return `${value.toFixed(decimals)} ${BYTE_UNITS[i]}`;
}

export function formatDate(date: string | Date): string {
  if (!date || (typeof date === "string" && date.trim() === "")) return "—";
  const d = typeof date === "string" ? new Date(date) : date;
  if (isNaN(d.getTime())) return "—";
  return d.toLocaleDateString("sv-SE", {
    year: "numeric",
    month: "short",
    day: "numeric",
    hour: "2-digit",
    minute: "2-digit",
  });
}

export function formatNumber(n: number | null | undefined): string {
  if (n == null) return "—";
  return n.toLocaleString();
}

const QUOTA_UNITS: Record<string, number> = {
  B: 1,
  KB: 1024,
  MB: 1024 ** 2,
  GB: 1024 ** 3,
  TB: 1024 ** 4,
  PB: 1024 ** 5,
};

export function parseQuotaBytes(quota: string): number | null {
  const match = quota.match(/([\d.]+)\s*(B|KB|MB|GB|TB|PB)/i);
  if (!match) return null;
  return parseFloat(match[1]) * (QUOTA_UNITS[match[2].toUpperCase()] ?? 1);
}

export interface ChargebackEntry {
  namespaceName?: string;
  startTime?: string;
  endTime?: string;
  objectCount?: number;
  ingestedVolume?: number;
  storageCapacityUsed?: number;
  bytesIn?: number;
  bytesOut?: number;
  reads?: number;
  writes?: number;
  deletes?: number;
  valid?: boolean;
}

export function buildStorageMap(
  chargeback: ChargebackEntry[],
): Map<string, number> {
  const map = new Map<string, number>();
  for (const entry of chargeback) {
    if (entry.namespaceName) {
      map.set(entry.namespaceName, entry.storageCapacityUsed ?? 0);
    }
  }
  return map;
}

export function getStorageUsed(
  chargeback: ChargebackEntry[],
  name: string,
): number {
  const entry = chargeback.find((e) => e.namespaceName === name);
  return entry?.storageCapacityUsed ?? 0;
}

export function calcQuotaPercent(
  used: number,
  quotaStr: string | null | undefined,
): number | null {
  if (!quotaStr) return null;
  const quotaBytes = parseQuotaBytes(quotaStr);
  if (!quotaBytes || !used) return null;
  return (used / quotaBytes) * 100;
}

const DATE_FILTER_MS: Record<string, number> = {
  "24h": 86_400_000,
  "7d": 604_800_000,
  "30d": 2_592_000_000,
};

export function matchesDateFilter(
  date: string | Date,
  filter: string,
): boolean {
  if (!filter) return true;
  const d = typeof date === "string" ? new Date(date) : date;
  const maxAge = DATE_FILTER_MS[filter];
  if (!maxAge) return true;
  return Date.now() - d.getTime() <= maxAge;
}

export function arraysEqual(a: string[], b: string[]): boolean {
  if (a.length !== b.length) return false;
  return a.every((v, i) => v === b[i]);
}

export function formatRelative(date: string | Date): string {
  const d = typeof date === "string" ? new Date(date) : date;
  const now = new Date();
  const diff = now.getTime() - d.getTime();
  const seconds = Math.floor(diff / 1000);
  const minutes = Math.floor(seconds / 60);
  const hours = Math.floor(minutes / 60);
  const days = Math.floor(hours / 24);

  if (days > 0) return `${days}d ago`;
  if (hours > 0) return `${hours}h ago`;
  if (minutes > 0) return `${minutes}m ago`;
  return "just now";
}
