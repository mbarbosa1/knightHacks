/**
 * Where the map gets its data.
 *
 * Today: files exported by the simulator into public/sim (`python -m sim.export --seed 42`).
 * Later: set VITE_API_BASE (the FastAPI backend) and the static layers come from its bulk
 * endpoints instead. The files have the same shape as those endpoints, so nothing else changes.
 *
 *   VITE_API_BASE   e.g. http://127.0.0.1:8000   backend: map layers and operational state
 *   VITE_SIM_BASE   default http://127.0.0.1:8100   simulator service: new earthquakes and their truth
 *                   (cd simulation && ../.venv/bin/python -m uvicorn sim.server:app --port 8100)
 *   VITE_RUN_ID     the run to show in the operational view
 */
import type { FeatureCollection, LineString, Point, Polygon } from "geojson";

const env = import.meta.env;
const API_BASE: string | undefined = env.VITE_API_BASE;
const SIM_BASE: string = env.VITE_SIM_BASE ?? "http://127.0.0.1:8100";
const MAP_VERSION: string = env.VITE_MAP_VERSION ?? "shinjuku-v1";
const RUN_KEY = "knighthacks-active-run";
function rememberedRun(): string | undefined {
  try { return window.sessionStorage.getItem(RUN_KEY) ?? undefined; } catch { return undefined; }
}
export const RUN_ID: string | undefined = env.VITE_RUN_ID ?? rememberedRun();

/** Stop following the remembered run (Reset), so a reload does not bring its units back either. */
export function forgetRun(): void {
  try { window.sessionStorage.removeItem(RUN_KEY); } catch { /* storage may be disabled */ }
}

export type DamageLevel = "none" | "light" | "moderate" | "severe" | "collapsed";
export type RoadStatus = "OPEN" | "LIMITED" | "BLOCKED";

export interface BuildingProps {
  id: string;
  height_m: number | null;
  floors: number | null;
  usage: string | null;
  structure: string | null;
  name_en: string | null;
  address_en: string | null;
}

export interface RoadProps {
  id: string;
  name_en: string | null;
}

export interface Facility {
  id: string;
  type: "hospital" | "fire_station" | "police" | "shelter";
  name_en: string | null;
  geometry: Point;
}

export interface StaticMap {
  buildings: FeatureCollection<Polygon, BuildingProps>;
  roads: FeatureCollection<LineString, RoadProps>;
  facilities: Facility[];
}

/** Hidden ground truth for one run. Only the operator's reveal-truth view may load this. */
export interface RunTruth {
  run_id: string;
  ingestion?: { ready: boolean; stored_observations?: number; stored_resources?: number };
  seed: number;
  scenario: { mw: number; depth_km: number; local_time?: string };
  intensity: { min: number; max: number; label: string };
  /** Buildings with no damage are left out. */
  building_damage: Record<string, { damage_level: DamageLevel; intensity: number }>;
  /** Roads that are not listed are OPEN. */
  road_status: Record<string, { status: RoadStatus; cause: string | null }>;
  people_by_building: Record<string, Partial<Record<"injured" | "trapped" | "critical" | "deceased", number>>>;
  summary: {
    buildings: Partial<Record<DamageLevel, number>>;
    roads: Partial<Record<RoadStatus, number>>;
    people: Record<string, number>;
    reports: number;
  };
}

/** What agents and operators believe, from the backend's run endpoints. Empty until a run is connected. */
export interface OperationalState {
  incidents: {
    id: string;
    status: string;
    building_id: string | null;
    location: Point | null;
    person_groups: { group_id: string; est_count: number; condition: string | null; needs: string[] }[] | null;
    /** Ids of the reports the agents based this incident on, and of those that speak against it. */
    evidence: { supports: string[]; contradicts: string[] } | null;
  }[];
  roadRestrictions: { road_id: string; status: RoadStatus }[];
  resources: { id: string; type: string; name: string | null; status: string; location: Point | null }[];
  /** Dispatch decisions: the response agent's proposals (status "draft", awaiting the operator) and units sent. */
  decisions: Decision[];
}

export interface Decision {
  id: string;
  incident_id: string | null;
  version: number;
  assignments: { resource_id: string; type?: string | null; why?: string }[] | null;
  eta_s: number | null;
  status: "draft" | "approved" | "invalidated" | "sent";
  invalidated_reason: string | null;
}

export const EMPTY_OPERATIONAL: OperationalState = { incidents: [], roadRestrictions: [], resources: [], decisions: [] };

export type Priority = "critical" | "high" | "medium" | "low";

/** How urgent an incident is, from the people the agents believe are there: anyone critical or trapped first,
 *  then injured, then damage with no casualties known; a disputed incident last. Lower rank = more urgent. */
export function incidentPriority(incident: OperationalState["incidents"][number]):
    { priority: Priority; rank: number; trapped: number; critical: number; injured: number } {
  const count = (c: string) => (incident.person_groups ?? [])
    .filter((g) => g.condition === c).reduce((n, g) => n + (g.est_count || 0), 0);
  const trapped = count("trapped"), critical = count("critical"), injured = count("injured");
  const priority: Priority = incident.status === "disputed" ? "low"
    : critical || trapped ? "critical" : injured ? "high" : incident.status === "resolved" ? "low" : "medium";
  const rank = ["critical", "high", "medium", "low"].indexOf(priority) * 1000 - (critical * 3 + trapped * 2 + injured);
  return { priority, rank, trapped, critical, injured };
}

/** Approve a proposal: the simulator sends the unit (on the roads believed open), then the draft is marked
 *  approved. Returns null, or the reason it could not go (unit no longer free, no way there). */
export async function approveProposal(runId: string, decision: Decision,
                                      incident: OperationalState["incidents"][number]): Promise<string | null> {
  const unit = decision.assignments?.[0]?.resource_id;
  const place = incident.building_id ? { building_id: incident.building_id }
    : incident.location ? { lon: incident.location.coordinates[0], lat: incident.location.coordinates[1] } : null;
  if (!unit || !place) return "This proposal has no unit or the incident has no location.";
  const sent = await fetch(`${SIM_BASE}/dispatch`, {
    method: "POST", headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ unit_id: unit, incident_id: incident.id, run_id: runId, ...place }),
  });
  if (!sent.ok) {
    const detail = (await sent.json().catch(() => ({}))).detail;
    await closeProposal(runId, decision, "invalidated", `Could not dispatch: ${detail ?? sent.statusText}`);
    return typeof detail === "string" ? detail : "The simulator could not send this unit.";
  }
  await closeProposal(runId, decision, "approved", null);
  return null;
}

export const rejectProposal = (runId: string, decision: Decision) =>
  closeProposal(runId, decision, "invalidated", "Rejected by the operator");

async function closeProposal(runId: string, d: Decision, status: Decision["status"], reason: string | null) {
  if (!API_BASE) return;
  await fetch(`${API_BASE}/runs/${runId}/decisions/${d.id}`, {
    method: "PUT", headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ incident_id: d.incident_id, version: d.version, assignments: d.assignments ?? [],
                           eta_s: d.eta_s, status, invalidated_reason: reason }),
  });
}

async function getJson<T>(url: string): Promise<T> {
  const response = await fetch(url);
  if (!response.ok) throw new Error(`${response.status} ${response.statusText} for ${url}`);
  return (await response.json()) as T;
}

export async function loadStaticMap(): Promise<StaticMap> {
  const base = API_BASE ? `${API_BASE}/map/versions/${MAP_VERSION}` : "/sim/map";
  const file = (name: string, ext: string) => (API_BASE ? `${base}/${name}` : `${base}/${name}.${ext}`);
  const [buildings, roads, facilities] = await Promise.all([
    getJson<StaticMap["buildings"]>(file("buildings", "geojson")),
    getJson<StaticMap["roads"]>(file("roads", "geojson")),
    getJson<Facility[]>(file("facilities", "json")),
  ]);
  return { buildings, roads, facilities };
}

/**
 * The run to show when the page opens: the one the simulator is playing right now (so the page always shows
 * what the agents are working on, even after a reload or when the earthquake was generated in another tab),
 * else the one this tab last generated or VITE_RUN_ID. Null when there is none: start one with Generate.
 */
export async function loadRunTruth(): Promise<RunTruth | null> {
  const live = await getJson<{ run_id?: string | null }>(`${SIM_BASE}/clock`).then((c) => c.run_id ?? undefined, () => undefined);
  const runId = live ?? RUN_ID;
  if (!runId) return null;
  try {
    const truth = await getJson<RunTruth>(`${SIM_BASE}/runs/${runId}/truth`);
    try { window.sessionStorage.setItem(RUN_KEY, runId); } catch { /* storage may be disabled */ }
    return truth;
  } catch {
    return null;
  }
}

export const MAGNITUDE_RANGE = { min: 5.8, max: 8.0, step: 0.1 };

/** Is the simulator service running? The earthquake controls are disabled when it is not. */
export async function simulatorAvailable(): Promise<boolean> {
  try {
    await getJson(`${SIM_BASE}/health`);
    return true;
  } catch {
    return false;
  }
}

/**
 * Ask the simulator for a new earthquake. Every call uses a new random seed, so depth, epicentre and
 * which buildings fail differ each time. magnitude null lets the seed pick the magnitude too.
 */
export class SubmissionError extends Error {
  constructor(message: string, public runId: string | null = null, public retryable = false) {
    super(message);
  }
}

async function submitEarthquake(url: string, body: unknown): Promise<RunTruth> {
  const response = await fetch(url, {
    method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body),
  });
  const result = await response.json();
  if (!response.ok) {
    const detail = result.detail;
    throw new SubmissionError(typeof detail === "string" ? detail : detail?.message ?? "Simulation could not be saved.",
                              detail?.run_id ?? null, detail?.retryable ?? false);
  }
  if (!result.ingestion?.ready) {
    throw new SubmissionError("This run was generated locally and has not been saved to Neon.");
  }
  try { window.sessionStorage.setItem(RUN_KEY, result.run_id); } catch { /* storage may be disabled */ }
  return result as RunTruth;
}

export async function createEarthquake(magnitude: number | null, speed: number = DEFAULT_SPEED): Promise<RunTruth> {
  return submitEarthquake(`${SIM_BASE}/runs`, magnitude === null ? { speed } : { mw: magnitude, speed });
}

/** How many simulated seconds pass per real second. */
export const SPEEDS = [5, 10] as const;
export const DEFAULT_SPEED = 10;   // faster than this, a big earthquake's reports reach Neon late

/** The live run's clock, kept by the simulator. run_id is null when nothing is playing. */
export interface RunClock {
  run_id: string | null;
  sim_time_s?: number;
  end_s?: number;
  speed?: number;
  playing: boolean;
  finished: boolean;
  reports_released?: number;
  reports_total?: number;
  last_error?: string | null;
  /** People the units have freed and treated so far, and people who died waiting. */
  rescued?: number;
  treated?: number;
  died_waiting?: number;
  /** How many units are in each state. */
  units?: { available: number; en_route: number; busy: number } | null;
}

/** One responder unit as the simulator has it right now. */
export interface LiveUnit {
  id: string;
  name: string;
  type: "fire_engine" | "ambulance" | "police";
  status: "available" | "en_route" | "busy";
  lon: number;
  lat: number;
  /** The road ahead while it drives; null otherwise. */
  route: [number, number][] | null;
  /** null until the unit has been sent somewhere. */
  destination: { lon: number; lat: number; building_id: string | null; incident_id: string | null } | null;
  /** The plan it follows. reason is set when the plan replaced one that a blocked road ended. */
  decision: { id: string; version: number; reason: string | null } | null;
  helped: number;
}

/** Where every unit of the live run is. Empty when nothing is playing. */
export async function loadUnits(): Promise<LiveUnit[]> {
  try {
    return await getJson<LiveUnit[]>(`${SIM_BASE}/units`);
  } catch {
    return [];
  }
}

export async function loadClock(): Promise<RunClock | null> {
  try {
    return await getJson<RunClock>(`${SIM_BASE}/clock`);
  } catch {
    return null;
  }
}

async function postSim(path: string, body: unknown): Promise<RunClock | null> {
  try {
    const response = await fetch(`${SIM_BASE}${path}`, {
      method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body),
    });
    return response.ok ? ((await response.json()) as RunClock) : null;
  } catch {
    return null;
  }
}

export const changeClock = (change: { speed?: number; playing?: boolean }) => postSim("/clock", change);
/** Stop the live run and close it. */
export const stopRun = () => postSim("/stop", {});

export async function retryEarthquake(runId: string): Promise<RunTruth> {
  return submitEarthquake(`${SIM_BASE}/runs/${runId}/retry`, {});
}

/** True once the backend and a run id are configured; until then the operational view is a placeholder. */
export const operationalConnected = Boolean(API_BASE);

export async function loadOperationalState(runId: string | undefined = RUN_ID): Promise<OperationalState> {
  if (!operationalConnected || !runId) return EMPTY_OPERATIONAL;
  const run = `${API_BASE}/runs/${runId}`;
  // Field names follow the backend README; not yet exercised against a live run.
  const [incidents, roadRestrictions, resources, decisions] = await Promise.all([
    getJson<OperationalState["incidents"]>(`${run}/incidents?limit=1000`),
    getJson<OperationalState["roadRestrictions"]>(`${run}/road-restrictions?limit=1000`),
    getJson<OperationalState["resources"]>(`${run}/resources?limit=1000`),
    getJson<Decision[]>(`${run}/decisions?limit=1000`).catch(() => [] as Decision[]),
  ]);
  return { incidents, roadRestrictions, resources, decisions };
}

/** A report as the backend's feed returns it (GET /runs/{id}/observations): what agents see, nothing more. */
export interface Report {
  id: string;
  channel: string;
  original_text: string | null;
  text_en: string | null;
  lang: string | null;
  claimed_location: string | null;
  location_precision: string | null;
  source_reliability: number | null;
  sim_time_s: number | null;
  longitude: number | null;
  latitude: number | null;
}

/** Reports that arrived after `after` (a report id), oldest first. The feed is a cursor, so each poll only brings the new ones. */
export async function loadReportsSince(runId: string, after?: string): Promise<Report[]> {
  if (!API_BASE) return [];
  const out: Report[] = [];
  let cursor = after;
  for (let page = 0; page < 5; page++) {
    const batch = await getJson<Report[]>(`${API_BASE}/runs/${runId}/observations?limit=1000${cursor ? `&after=${cursor}` : ""}`);
    out.push(...batch);
    if (batch.length < 1000) break;
    cursor = batch[batch.length - 1].id;
  }
  return out;
}

/** Drone recon (simulator POST /recon): the real GSI aerial photo of a place, a SIMULATED post-earthquake drone
 *  image of it, and the vision model's reading. Image paths are on the simulator service. */
export interface ReconResult {
  id: string;
  run_id: string;
  incident_id: string | null;
  longitude: number;
  latitude: number;
  sim_time_s: number;
  before: string;
  after: string;
  candidates: { label: string; building_id: string; name: string | null; usage: string | null; floors: number | null }[];
  reading: {
    damage_seen: "none" | "light" | "moderate" | "severe" | "collapsed" | null;
    /** The model's call for each lettered building (before vs after). */
    buildings: Record<string, string>;
    building: string | null;
    collapse_visible: boolean;
    debris_on_streets: { where?: string; passable?: string }[];
    approach: string | null;
    staging: string | null;
    confidence: number | null;
    summary: string | null;
  };
  matched_building_id: string | null;
  seconds: number;
  report_id?: string;
}

export const reconImage = (path: string) => `${SIM_BASE}${path}`;

async function simPost<T>(path: string, body?: unknown): Promise<T> {
  const r = await fetch(`${SIM_BASE}${path}`, {
    method: "POST", headers: { "Content-Type": "application/json" }, body: body ? JSON.stringify(body) : undefined,
  });
  if (!r.ok) {
    const detail = await r.json().then((j) => j.detail, () => r.statusText);
    throw new Error(typeof detail === "string" ? detail : `HTTP ${r.status}`);
  }
  return r.json() as Promise<T>;
}

export function flyRecon(place: { incident_id: string; building_id: string | null; lon: number; lat: number; context: string }) {
  return simPost<ReconResult>("/recon", place);
}

export function sendReconReport(reconId: string) {
  return simPost<{ report_id: string; text: string }>(`/recon/${reconId}/report`);
}
