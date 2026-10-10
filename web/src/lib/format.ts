// Display formats shared by the views. Every number the UI shows goes through one of these.

const UNITS = ["B", "kB", "MB", "GB", "TB"];

export function bytes(n: number | null | undefined): string {
  if (n === null || n === undefined) return "–";
  let value = Math.abs(n);
  let unit = 0;
  while (value >= 1024 && unit < UNITS.length - 1) {
    value /= 1024;
    unit += 1;
  }
  const sign = n < 0 ? "−" : "";
  return `${sign}${value < 10 && unit > 0 ? value.toFixed(1) : Math.round(value)} ${UNITS[unit]}`;
}

export function signedBytes(n: number): string {
  return n > 0 ? `+${bytes(n)}` : bytes(n);
}

export function count(n: number | null | undefined): string {
  return n === null || n === undefined ? "–" : n.toLocaleString("en");
}

export function ms(n: number | null | undefined): string {
  if (n === null || n === undefined) return "–";
  return n >= 1000 ? `${(n / 1000).toLocaleString("en", { maximumFractionDigits: 1 })} s` : `${Math.round(n)} ms`;
}

/** A plan cost, compact: 8.4e6 reads better than 8,400,000 next to a gate limit. */
export function cost(n: number | null | undefined): string {
  if (n === null || n === undefined) return "–";
  return n >= 100_000 ? n.toExponential(1).replace("+", "") : n.toLocaleString("en", { maximumFractionDigits: 1 });
}

/** Unknown is never shown as zero. */
export function usd(n: number | null | undefined): string {
  if (n === null || n === undefined) return "unknown";
  return `$${n.toFixed(n < 1 ? 4 : 2)}`;
}

export function percent(share: number): string {
  return `${Math.round(share * 100)}%`;
}

export function dateTime(iso: string | null | undefined): string {
  if (!iso) return "–";
  return new Date(iso).toLocaleString("en-GB", {
    day: "numeric",
    month: "short",
    hour: "2-digit",
    minute: "2-digit",
  });
}

/** How long ago, coarse: the probe's ages and Thread activity. */
export function ago(iso: string | null | undefined): string {
  if (!iso) return "never";
  const hours = (Date.now() - new Date(iso).getTime()) / 3_600_000;
  if (hours < 1 / 60) return "just now";
  if (hours < 1) return `${Math.round(hours * 60)} min ago`;
  if (hours < 48) return `${hours.toFixed(1)} h ago`;
  return `${Math.round(hours / 24)} days ago`;
}

/** One line of at most `max` characters, cut with an ellipsis. */
export function truncate(text: string, max: number): string {
  const flat = text.split(/\s+/).join(" ").trim();
  return flat.length > max ? `${flat.slice(0, max - 1)}…` : flat;
}

/** A collection's name as the API and the CLI write it: schema-qualified when it has a schema. */
export function qualified(ref: { namespace: string | null; name: string }): string {
  return ref.namespace ? `${ref.namespace}.${ref.name}` : ref.name;
}

export function shortId(id: string): string {
  return id.slice(0, 8);
}

/** A stats window's length, e.g. 3 h 20 min. */
export function duration(seconds: number | null | undefined): string {
  if (seconds === null || seconds === undefined) return "–";
  const hours = Math.floor(seconds / 3600);
  const minutes = Math.round((seconds % 3600) / 60);
  if (hours >= 48) return `${Math.round(hours / 24)} days`;
  return hours ? `${hours} h ${minutes} min` : `${minutes} min`;
}
