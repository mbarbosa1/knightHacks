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
  incidents: { id: string; status: string; building_id: string | null; location: Point | null }[];
  roadRestrictions: { road_id: string; status: RoadStatus }[];
  resources: { id: string; type: string; name: string | null; status: string; location: Point | null }[];
}

export const EMPTY_OPERATIONAL: OperationalState = { incidents: [], roadRestrictions: [], resources: [] };

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
 * The truth of a run that already exists. The page opens on the plain city, so this only returns
 * something when a run was asked for by id (VITE_RUN_ID); otherwise an earthquake starts with Generate.
 */
export async function loadRunTruth(): Promise<RunTruth | null> {
  if (!RUN_ID) return null;
  try {
    return await getJson<RunTruth>(`${SIM_BASE}/runs/${RUN_ID}/truth`);
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
export const SPEEDS = [10, 20] as const;
export const DEFAULT_SPEED = 20;

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
  const [incidents, roadRestrictions, resources] = await Promise.all([
    getJson<OperationalState["incidents"]>(`${run}/incidents?limit=1000`),
    getJson<OperationalState["roadRestrictions"]>(`${run}/road-restrictions?limit=1000`),
    getJson<OperationalState["resources"]>(`${run}/resources?limit=1000`),
  ]);
  return { incidents, roadRestrictions, resources };
}
