export const money = (value: number) =>
  new Intl.NumberFormat("en-US", { style: "currency", currency: "USD", maximumFractionDigits: 0 }).format(value);

export const metres = (value: number) => `${Math.round(value * 100) / 100} m`;

export const humanize = (category: string | null) => (category ?? "item").replaceAll("_", " ");

export const plural = (count: number, word: string) => `${count} ${word}${count === 1 ? "" : "s"}`;

export function clock(seconds: number) {
  const s = Math.max(0, Math.floor(seconds));
  return `${Math.floor(s / 60)}:${String(s % 60).padStart(2, "0")}`;
}

/** Collapse repeated products (four of the same chair) into one entry with a count. */
export function grouped<T extends { asset_id: string }>(items: T[]): (T & { count: number })[] {
  const byId = new Map<string, T & { count: number }>();
  for (const item of items) {
    const entry = byId.get(item.asset_id);
    if (entry) entry.count += 1;
    else byId.set(item.asset_id, { ...item, count: 1 });
  }
  return [...byId.values()];
}
