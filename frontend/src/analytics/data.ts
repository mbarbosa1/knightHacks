export type Row = Record<string, unknown>;
export interface Scenario extends Row { id: string; started_at?: string; ended_at?: string; event_count: number; score_count: number; }
export interface Bundle { run_id: string; source: string; complete: boolean; loaded_at: number; datasets: Record<string, Row[]>; }
export interface Connection { configured: boolean; workspace_url: string | null; genie_configured: boolean; }
export interface GenieReply { text: string; queries: { description: string; sql: string; rows: Row[] }[]; source: string; }
export const BASE = (import.meta.env?.VITE_API_BASE ?? "http://127.0.0.1:8000").replace(/\/$/, "") + "/analytics/databricks";
export async function read<T>(path: string, signal?: AbortSignal, body?: unknown): Promise<T> {
  const response = await fetch(BASE + path, { signal, ...(body ? { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) } : {}) });
  let result;
  try { result = await response.json(); } catch { throw new Error("The analytics service returned an unreadable response."); }
  if (!response.ok) throw new Error(typeof result.detail === "string" ? result.detail : "The analytics service could not complete the request.");
  return result as T;
}
export function askGenie(run_id: string, question: string, signal?: AbortSignal) {
  return read<GenieReply>("/ask", signal, { run_id, question });
}
export const text = (value: unknown): string => value === null || value === undefined ? "—" : typeof value === "object" ? JSON.stringify(value) : String(value);
export const number = (value: unknown): number | null => value === null || value === undefined || value === "" ? null : Number.isFinite(Number(value)) ? Number(value) : null;
export const object = (value: unknown): Row => value && typeof value === "object" && !Array.isArray(value) ? value as Row : {};
export const label = (value: unknown) => text(value).replace(/_/g, " ");
export const clock = (seconds: number) => `${Math.floor(Math.max(0, seconds) / 60)}:${String(Math.floor(Math.max(0, seconds)) % 60).padStart(2, "0")}`;
export function timestamp(value: unknown): number | null {
  if (typeof value !== "string") return null;
  // Warehouse timestamps are UTC but some SQL results omit the zone.
  const normalized = /(?:Z|[+-]\d\d:\d\d)$/.test(value) ? value : value.replace(" ", "T") + "Z";
  const result = Date.parse(normalized);
  return Number.isFinite(result) ? result : null;
}
export function date(value: unknown): string { const t = timestamp(value); return t === null ? "Time unknown" : new Date(t).toLocaleString(); }
export function eventRow(event: Row): Row {
  const payload = object(event.payload);
  return payload.row && typeof payload.row === "object" ? object(payload.row) : payload;
}
export function eventTime(event: Row, origin: number): number | null {
  const created = timestamp(event.created_at);
  return created === null ? null : Math.max(0, (created - origin) / 1000);
}
export function replayInfo(bundle: Bundle) {
  const events = bundle.datasets.events ?? [];
  const times = events.map(e => timestamp(e.created_at)).filter((t): t is number => t !== null);
  const scenario = bundle.datasets.scenarios?.[0] ?? {};
  const origin = timestamp(scenario.started_at) ?? (times.length ? Math.min(...times) : 0);
  const duration = times.length ? Math.max(0, (Math.max(...times) - origin) / 1000) : 0;
  return { origin, duration, events: [...events].sort((a, b) => (timestamp(a.created_at) ?? Infinity) - (timestamp(b.created_at) ?? Infinity) || text(a.event_id).localeCompare(text(b.event_id))) };
}
export function snapshot(bundle: Bundle, at: number | null) {
  const info = replayInfo(bundle);
  const tables: Record<string, Map<string, Row>> = {};
  const visibleEvents: Row[] = [];
  let simTime: number | null = null;
  let unknownTimes = 0;
  for (const event of info.events) {
    const elapsed = eventTime(event, info.origin);
    if (elapsed === null) unknownTimes++;
    if (at !== null && (elapsed === null || elapsed > at)) continue;
    visibleEvents.push(event);
    const payload = object(event.payload);
    const row = eventRow(event);
    const sim = number(payload.sim_time_s) ?? number(row.sim_time_s);
    if (sim !== null) simTime = Math.max(simTime ?? 0, sim);
    const table = text(event.table_name);
    tables[table] ??= new Map();
    const id = text(event.row_id);
    if (text(payload.operation).toUpperCase() === "DELETE") tables[table].delete(id);
    else tables[table].set(id, { ...row, id: row.id ?? id, _recorded_at: event.created_at });
  }
  const rows = Object.fromEntries(Object.entries(tables).map(([k, v]) => [k, Array.from(v.values())]));
  const latest = (dataset: string, key: string) => {
    const map = new Map<string, Row>();
    for (const row of bundle.datasets[dataset] ?? []) {
      const t = number(row.simulation_time_s);
      if (at !== null && (simTime === null || t === null || t > simTime)) continue;
      const id = text(row[key]);
      const prev = map.get(id);
      if (!prev || (t ?? -Infinity) >= (number(prev.simulation_time_s) ?? -Infinity)) map.set(id, row);
    }
    return [...map.values()];
  };
  return { rows, visibleEvents, simTime, unknownTimes, info,
    damage: latest("building_damage", "building_id"), roads: latest("road_status", "road_id"), people: latest("people", "id") };
}
export function csv(rows: Row[]): string {
  const keys = [...new Set(rows.flatMap(row => Object.keys(row)))];
  const cell = (value: unknown) => {
    let s = value === null || value === undefined ? "" : typeof value === "object" ? JSON.stringify(value) : String(value);
    // Prevent spreadsheet formula execution when users export report text.
    if (/^[=+@\-\t\r]/.test(s)) s = "'" + s;
    return '"' + s.replace(/"/g, '""') + '"';
  };
  return [keys.map(cell).join(","), ...rows.map(row => keys.map(key => cell(row[key])).join(","))].join("\r\n");
}
export function download(rows: Row[], name: string) {
  const url = URL.createObjectURL(new Blob([csv(rows)], { type: "text/csv;charset=utf-8" }));
  const link = document.createElement("a"); link.href = url; link.download = name; link.click(); URL.revokeObjectURL(url);
}
