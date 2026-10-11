import { lazy, Suspense, useEffect, useMemo, useRef, useState } from "react";
import {
  AlertTriangle,
  BarChart3,
  ArrowDownRight,
  ArrowUpRight,
  Check,
  ChevronRight,
  Clock3,
  Crosshair,
  Filter,
  ExternalLink,
  Layers,
  MapPin,
  Maximize2,
  Menu,
  Minus,
  Navigation,
  Plane,
  Radio,
  Search,
  Settings2,
  ShieldCheck,
  Signal,
  Square,
  Volume2,
  Waves,
  X,
  ZoomIn,
  ZoomOut,
} from "lucide-react";
import { type MockReport, type ReportKind } from "./data/mockReports";
import { CHANNELS, UNIT_COLOR, UNIT_LABEL, ShinjukuMap, formatClock, type ShinjukuMapHandle } from "./map/ShinjukuMap";
import type { MapState } from "./map/ShinjukuMap";
import { approveProposal, incidentPriority, loadAgentRuns, loadAgentsStatus, rejectProposal } from "./map/dataSource";
import type { AgentRun, AgentsStatus, Decision, LiveUnit, OperationalState, ReconResult, Report } from "./map/dataSource";
import { ReconPage } from "./Recon";
import "./analytics/analytics.css";

const AnalyticsPage = lazy(() => import("./analytics/AnalyticsPage"));
const currentPage = () => window.location.pathname.replace(/\/$/, "") === "/analytics" ? "analytics" : "operations";

const DBX = "https://dbc-87980e9b-e483.cloud.databricks.com", DBX_ORG = "7474645633187109";
const EMBED_URL = `${DBX}/embed/dashboardsv3/01f1c57b716915d8b00465003e793b28?o=${DBX_ORG}`;
const DASHBOARD_URL = `${DBX}/dashboardsv3/01f1c57b716915d8b00465003e793b28/published?o=${DBX_ORG}`;
const GENIE_URL = `${DBX}/genie/rooms/01f1c580620a1d308658e770ef4e127c?o=${DBX_ORG}`;
type BelievedIncident = OperationalState["incidents"][number];

/** One line of the agent activity feed: an agent's conclusion, or word from a unit in the field. */
interface Activity {
  key: string;
  at: number;                      // simulated seconds since the earthquake
  kind: "assess" | "respond" | "field" | "error";
  who: string;
  title: string;
  lines: string[];
  note?: string;
}
const AGENT_NAME: Record<string, string> = {
  triage: "Triage",
  detect_agent: "Detect agent",
  medical_agent: "Medical agent",
  response_agent: "Response agent",
};
const AGENTS_REFRESH_MS = 3000;

/** A count for a dashboard card: two digits, or a dash while no run is playing. */
const pad = (n: number | undefined | null) => (n === undefined || n === null ? "–" : String(n).padStart(2, "0"));

/** Units that are doing something come first: driving, then working, then free again, then never sent. */
const unitOrder = (unit: LiveUnit) =>
  unit.status === "en_route" ? 0 : unit.status === "busy" ? 1 : unit.destination ? 2 : 3;
const unitState = (unit: LiveUnit) =>
  unit.status === "en_route" ? "On the way" : unit.status === "busy" ? "On scene" : unit.destination ? "Free" : "At station";

/** The people the agents believe are at an incident, e.g. "4 trapped, 1 critical". */
function believedPeople(incident: BelievedIncident): string {
  const groups = (incident.person_groups ?? []).filter((g) => g.est_count > 0);
  return groups.map((g) => `${g.est_count} ${g.condition ?? "people"}`).join(", ") || "nobody counted yet";
}

/**
 * A real report from the backend (what agents see) in the dashboard's report shape. Radio = 119 calls,
 * eyewitness texts and fire/police radio; Social = social media (CHANNEL_GROUP in simulation/sim/reports.py).
 * Time is simulation time since the earthquake, not a clock time.
 */
function toReport(o: Report): MockReport {
  const t = Math.max(0, Math.round(o.sim_time_s ?? 0));
  const text = o.text_en ?? o.original_text ?? "";
  return {
    id: `R-${o.id.slice(0, 6).toUpperCase()}`,
    kind: o.channel === "social_media" ? "Social" : "Radio",
    time: `T+${String(Math.floor(t / 60)).padStart(2, "0")}:${String(t % 60).padStart(2, "0")}`,
    status: text.startsWith("RT ") ? "Replay" : "Processed",
    text,
    location: o.claimed_location ?? (o.location_precision === "gps" ? "GPS location" : "Location unclear"),
    verified: false,
    incidentId: "",
    lang: o.lang ?? undefined,
    originalText: o.lang && o.lang !== "en" ? o.original_text ?? undefined : undefined,
    observationId: o.id,
    channel: o.channel,
    precision: o.location_precision ?? undefined,
    reliability: o.source_reliability ?? undefined,
    longitude: o.longitude ?? undefined,
    latitude: o.latitude ?? undefined,
  };
}

function App() {
  const [filter, setFilter] = useState<"All" | ReportKind>("All");
  const [search, setSearch] = useState("");
  const [selectedReportId, setSelectedReportId] = useState<string | null>(null);
  const [reportOpen, setReportOpen] = useState(false);
  const [utc, setUtc] = useState(new Date());
  const mapRef = useRef<ShinjukuMapHandle>(null);
  const mapPanel = useRef<HTMLElement>(null);
  const [fullscreen, setFullscreen] = useState(false);
  useEffect(() => {
    const changed = () => setFullscreen(document.fullscreenElement === mapPanel.current);
    document.addEventListener("fullscreenchange", changed);
    return () => document.removeEventListener("fullscreenchange", changed);
  }, []);
  const toggleFullscreen = () => {
    if (document.fullscreenElement) document.exitFullscreen();
    else mapPanel.current?.requestFullscreen();
  };
  // The map follows the saved run and collects its reports as the live clock releases them into Neon
  // (Gemini-worded when the simulator runs with SIM_GEMINI=1). The feed shows the same list, so it fills in one
  // by one. With no saved run on the map, the feed is empty.
  const [mapState, setMapState] = useState<MapState | null>(null);
  const observations: Report[] | null = mapState?.truth?.ingestion?.ready ? mapState.reports : null;
  const reports: MockReport[] = useMemo(
    () => observations
      ? [...observations].sort((a, b) => (b.sim_time_s ?? 0) - (a.sim_time_s ?? 0)).map(toReport)   // newest first
      : [],
    [observations],
  );
  const selectedReport: MockReport | null =
    reports.find((report) => report.id === selectedReportId) ?? null;
  // The incident the agents built from this report (or that it speaks against), if they have got to it.
  const obsId = selectedReport?.observationId;
  const linked: BelievedIncident | null =
    (obsId && mapState?.operational.incidents.find(
      (i) => i.evidence?.supports.includes(obsId) || i.evidence?.contradicts.includes(obsId))) || null;
  const against = Boolean(obsId && linked?.evidence?.contradicts.includes(obsId));
  const reportState = linked ? linked.status.toUpperCase() : "NOT ASSESSED YET";

  const clock = mapState?.clock ?? null;
  const truth = mapState?.truth ?? null;
  const runState = !truth
    ? "NO RUN"
    : !clock
      ? "RUN NOT PLAYING"
      : clock.finished ? "RUN FINISHED" : clock.playing ? `RUNNING ${clock.speed}x` : "PAUSED";
  const units = useMemo(
    () => [...(mapState?.units ?? [])].sort((a, b) => unitOrder(a) - unitOrder(b) || a.name.localeCompare(b.name)),
    [mapState?.units],
  );
  const driving = units.filter((unit) => unit.status === "en_route");
  const roadsRestricted = mapState?.operational.roadRestrictions.filter((road) => road.status !== "OPEN").length ?? 0;
  // The priority queue: the incidents the agents built, most urgent first, with what was proposed and sent.
  const runId = clock?.run_id ?? truth?.run_id ?? null;
  const decisions: Decision[] = mapState?.operational.decisions ?? [];
  const unitName = (id: string) => units.find((u) => u.id === id)?.name ?? "A unit";
  const queue = useMemo(
    () => (mapState?.operational.incidents ?? [])
      .filter((i) => i.status !== "resolved")
      .map((i) => ({ incident: i, ...incidentPriority(i),
                     waiting: decisions.some((d) => d.status === "draft" && d.incident_id === i.id) }))
      .sort((a, b) => Number(b.waiting) - Number(a.waiting) || a.rank - b.rank),
    [mapState?.operational.incidents, decisions],
  );
  const pending = decisions.filter((d) => d.status === "draft");
  // Finished jobs and incidents the operator cleared from the panels (this tab only; nothing is deleted).
  const [dismissed, setDismissed] = useState<Set<string>>(() => {
    try { return new Set(JSON.parse(window.sessionStorage.getItem("kyu-dismissed") ?? "[]")); } catch { return new Set(); }
  });
  const dismiss = (key: string) => setDismissed((old) => {
    const next = new Set(old).add(key);
    try { window.sessionStorage.setItem("kyu-dismissed", JSON.stringify([...next])); } catch { /* storage off */ }
    return next;
  });
  const jobKey = (u: LiveUnit) => `job:${u.id}:${u.decision?.id ?? ""}`;
  const jobsFor = (incidentId: string) => units.filter((u) => u.destination?.incident_id === incidentId);
  const [acting, setActing] = useState<Record<string, string>>({});   // decision id -> "sending" | error text
  const decide = async (d: Decision, incident: BelievedIncident, approve: boolean) => {
    if (!runId) return;
    setActing((a) => ({ ...a, [d.id]: approve ? "sending" : "rejecting" }));
    const problem = approve ? await approveProposal(runId, d, incident) : (await rejectProposal(runId, d), null);
    setActing((a) => ({ ...a, [d.id]: problem ?? "done" }));
    // Approved: follow the vehicle as it sets off on its route.
    const unit = approve && !problem ? units.find((u) => u.id === d.assignments?.[0]?.resource_id) : null;
    if (unit) mapRef.current?.focus(unit.lon, unit.lat);
  };
  // Drone recon page: kept beside the dashboard (which stays mounted, so the map keeps its state).
  // Pages: operations, drone recon (Arwa), analytics & replay (Camila), and the embedded Databricks dashboard (Arwa).
  const [page, setPage] = useState<"ops" | "recon" | "analytics" | "databricks">(currentPage() === "analytics" ? "analytics" : "ops");
  useEffect(() => {
    document.title = page === "analytics" ? "Analytics & Replay · KYU-BOT" : "KYU-BOT · Disaster Intelligence";
    const path = page === "analytics" ? "/analytics" : "/";
    if (window.location.pathname !== path) window.history.replaceState({}, "", path);
    if (page === "ops") window.dispatchEvent(new Event("resize"));   // the map was hidden: let it measure itself again
  }, [page]);
  const [reconFor, setReconFor] = useState<string | null>(null);
  const [recons, setRecons] = useState<Record<string, ReconResult>>({});
  const openRecon = (incidentId: string) => { setReconFor(incidentId); setPage("recon"); };
  const destinationName = (unit: LiveUnit) =>
    unit.destination
      ? (unit.destination.building_id && mapRef.current?.buildingName(unit.destination.building_id)) ||
        `${unit.destination.lat.toFixed(4)}° N, ${unit.destination.lon.toFixed(4)}° E`
      : "";
  // What the agents concluded, asked of the backend every few seconds. An entry is stamped with the simulated
  // time at which the dashboard first saw it, so it can sit in one timeline with the units' own reports.
  const [agentRuns, setAgentRuns] = useState<(AgentRun & { seenAt: number })[]>([]);
  const [agentsStatus, setAgentsStatus] = useState<AgentsStatus | null>(null);
  const simNow = useRef(0);
  simNow.current = clock?.sim_time_s ?? 0;
  useEffect(() => {
    setAgentRuns([]);
    setAgentsStatus(null);
    if (!runId) return;
    let stop = false;
    const tick = async () => {
      const [runs, status] = await Promise.all([loadAgentRuns(runId), loadAgentsStatus()]);
      if (stop) return;
      setAgentsStatus(status);
      setAgentRuns((previous) => {
        const seen = new Map(previous.map((run) => [run.id, run.seenAt]));
        return runs.map((run) => ({ ...run, seenAt: seen.get(run.id) ?? simNow.current }));
      });
    };
    tick();
    const timer = window.setInterval(tick, AGENTS_REFRESH_MS);
    return () => {
      stop = true;
      window.clearInterval(timer);
    };
  }, [runId]);
  const agentsLive = Boolean(runId && agentsStatus?.running && agentsStatus.run_id === runId);
  useEffect(() => {
    setSelectedReportId(null);
    setReportOpen(false);
    setSearch("");
  }, [runId]);

  const activity: Activity[] = useMemo(() => {
    const incidents = mapState?.operational.incidents ?? [];
    const place = (incidentId: string) => {
      const incident = incidents.find((i) => i.id === incidentId);
      if (!incident) return "an incident";
      const name = incident.building_id ? mapRef.current?.buildingName(incident.building_id) : null;
      return `${believedPeople(incident)}${name ? ` at ${name}` : ""}`;
    };
    const unitName = (unitId: string) => units.find((unit) => unit.id === unitId)?.name ?? "a unit";
    const out: Activity[] = [];
    for (const run of agentRuns) {
      const who = AGENT_NAME[run.agent];
      const output = run.output;
      if (!who || !output) continue;                       // the orchestrator's own bookkeeping is not shown
      const took = run.duration_ms ? `${Math.round(run.duration_ms / 1000)} s` : undefined;
      if (output.error) {
        out.push({ key: run.id, at: run.seenAt, kind: "error", who, title: "Could not finish this round", lines: [output.error.slice(0, 160)], note: took });
      } else if (run.agent === "response_agent") {
        const proposing = output.proposed !== undefined;   // approval mode: the operator sends them
        const sent = output.proposed ?? output.dispatched ?? [];
        const units_ = `${sent.length} unit${sent.length === 1 ? "" : "s"}`;
        out.push({
          key: run.id, at: run.seenAt, kind: "respond", who, note: took,
          title: !sent.length ? (proposing ? "Nothing new to propose" : "Sent nobody")
            : proposing ? `Proposed ${units_} · waiting for approval` : `Sent ${units_}`,
          lines: [
            ...sent.map((d) => `${unitName(d.unit_id)} → ${place(d.incident_id)}`),
            ...(output.refused ?? []).map((r) => `Could not send ${unitName(r.unit_id)}: ${r.reason}`),
            ...((output.not_covered?.length ?? 0) > 0 ? [`${output.not_covered!.length} need${output.not_covered!.length === 1 ? "" : "s"} left uncovered`] : []),
          ],
        });
      } else {
        const said = output.situation_summary ?? output.summary;
        const found = output.incidents?.length;
        out.push({
          key: run.id, at: run.seenAt, kind: "assess", who, note: took,
          title: found !== undefined ? `Assessed reports · ${found} incident${found === 1 ? "" : "s"}` : "Assessed reports",
          lines: said ? [said] : [],
        });
      }
    }
    for (const report of observations ?? []) {
      if (report.channel !== "responder_update") continue;
      out.push({ key: report.id, at: report.sim_time_s ?? 0, kind: "field", who: "From the field", title: report.text_en ?? "", lines: [] });
    }
    return out.sort((a, b) => b.at - a.at).slice(0, 40);
  }, [agentRuns, observations, units, mapState?.operational.incidents]);

  const visibleReports = useMemo(
    () =>
      reports.filter(
        (report) =>
          (filter === "All" || report.kind === filter) &&
          `${report.text} ${report.originalText ?? ""} ${report.location} ${report.kind}`
            .toLowerCase()
            .includes(search.toLowerCase()),
      ),
    [reports, filter, search],
  );
  useEffect(() => {
    const timer = window.setInterval(() => setUtc(new Date()), 1000);
    return () => window.clearInterval(timer);
  }, []);

  return (
    <div className="app-shell">
      <header className="topbar">
        <div className="brand">
          <div className="brand-mark">
            <KyuBotMark />
          </div>
          <div>
            <div className="brand-name">KYU-BOT</div>
            <div className="brand-caption">DISASTER INTELLIGENCE</div>
          </div>
        </div>
        <div className="scenario">
          <span className="eyebrow">{page === "ops" ? "OPERATIONS OVERVIEW" : page === "recon" ? "DRONE RECON" : page === "analytics" ? "ANALYTICS & REPLAY" : "DATABRICKS DASHBOARD"}</span>
          <span className="scenario-name">Tokyo earthquake scenario</span>
        </div>
        <nav className="page-tabs">
          <button className={page === "ops" ? "active" : ""} onClick={() => setPage("ops")}><MapPin size={13} /> Operations</button>
          <button className={page === "recon" ? "active" : ""} onClick={() => setPage("recon")}>
            <Plane size={13} /> Drone recon{Object.keys(recons).length ? <small>{Object.keys(recons).length}</small> : null}
          </button>
          <button className={page === "analytics" ? "active" : ""} onClick={() => setPage("analytics")}>
            <BarChart3 size={13} /> Analytics &amp; replay
          </button>
          <button className={page === "databricks" ? "active" : ""} onClick={() => setPage("databricks")}>
            <Layers size={13} /> Databricks
          </button>
        </nav>
        <div className="top-actions">
          <span className="live-badge">
            <i /> SIMULATION
          </span>
          <span className="clock">
            {clock?.sim_time_s !== undefined
              ? <>{formatClock(clock.sim_time_s)} <small>SINCE THE EARTHQUAKE</small></>
              : <>{utc.toISOString().slice(11, 19)} <small>UTC</small></>}
          </span>
          <button className="icon-button top-icon" aria-label="Audio">
            <Volume2 size={16} />
          </button>
          <button className="icon-button top-icon" aria-label="Settings">
            <Settings2 size={17} />
          </button>
        </div>
      </header>
      <div className="operations-view" hidden={page === "analytics"}>
      <div className="status-strip">
        <div>
          <span className="status-dot" /> {runState}{" "}
          <span className="strip-divider">/</span>{" "}
          {truth
            ? `M ${truth.scenario.mw.toFixed(1)} earthquake · Intensity ${truth.intensity.label} (Japan scale)`
            : "Generate an earthquake on the map to start"}
        </div>
      </div>
      {page === "databricks" && (
        <main className="databricks-page">
          <div className="analytics-bar">
            <span>History of every simulated earthquake: damage, people, reports and how well the agents did (Databricks).</span>
            <a className="recon-fly" href={GENIE_URL} target="_blank" rel="noreferrer">Ask Genie ↗</a>
            <a className="analytics-link" href={DASHBOARD_URL} target="_blank" rel="noreferrer">Open in Databricks ↗</a>
          </div>
          <iframe className="analytics-frame" src={EMBED_URL} title="Databricks dashboard" />
        </main>
      )}
      {page === "recon" && (
        <ReconPage queue={queue} reports={observations ?? []} decisions={decisions} results={recons}
          onResult={(id, r) => setRecons((all) => ({ ...all, [id]: r }))}
          selectedId={reconFor} onSelect={setReconFor} runId={runId}
          acting={acting} onDecide={decide} unitName={unitName}
          buildingName={(id) => mapRef.current?.buildingName(id)}
          onShowOnMap={(lon, lat) => { setPage("ops"); window.setTimeout(() => mapRef.current?.focus(lon, lat), 50); }} />
      )}
      <main className="dashboard" style={page === "ops" ? undefined : { display: "none" }}>
        <aside className="panel incoming-panel">
          <div className="panel-heading">
            <div>
              <Signal size={14} />
              <span>INCOMING INFORMATION</span>
            </div>
            <span className="count-badge">
              {visibleReports.length.toString().padStart(2, "0")}
            </span>
          </div>
          <div className="filter-row">
            {(["All", "Radio", "Social"] as const).map((item) => (
              <button
                key={item}
                className={`filter-pill ${filter === item ? "active" : ""}`}
                onClick={() => setFilter(item)}
              >
                {item}
                <span>
                  {item === "All"
                    ? reports.length
                    : reports.filter((report) => report.kind === item).length}
                </span>
              </button>
            ))}
          </div>
          <label className="search-box">
            <Search size={14} />
            <input
              value={search}
              onChange={(event) => setSearch(event.target.value)}
              placeholder="Search reports or locations..."
            />
            {search && (
              <button onClick={() => setSearch("")} aria-label="Clear search">
                <X size={13} />
              </button>
            )}
          </label>
          <div className="report-list">
            {visibleReports.map((report) => (
              <button
                key={report.id}
                className={`report-card ${selectedReportId === report.id ? "selected" : ""} ${report.kind.toLowerCase()}`}
                onClick={() => {
                  if (selectedReportId === report.id) setReportOpen(true);   // a second click opens the full report
                  setSelectedReportId(report.id);
                  if (report.longitude !== undefined && report.latitude !== undefined) {
                    mapRef.current?.focus(report.longitude, report.latitude);
                  }
                }}
              >
                <div className="report-top">
                  <div className="report-type">
                    {report.kind === "Social" ? (
                      <Square size={13} />
                    ) : (
                      <Radio size={13} />
                    )}
                    <strong>
                      {report.kind === "Radio"
                        ? "Field radio"
                        : `${report.kind} report`}
                    </strong>
                  </div>
                  <time>{report.time}</time>
                </div>
                <div className="report-meta">
                  <span className={`tiny-state ${report.status.toLowerCase()}`}>
                    {report.status}
                  </span>
                </div>
                <p>{report.text}</p>
                <div className="report-location">
                  <MapPin size={11} /> {report.location}
                  {report.verified && <Check size={11} />}
                </div>
              </button>
            ))}
            {visibleReports.length === 0 && (
              <div className="empty-state">
                {!observations
                  ? "No run is playing. Generate an earthquake on the map and its reports arrive here."
                  : reports.length === 0
                  ? "No reports yet. They arrive as the simulation clock runs."
                  : "No reports match this search."}
              </div>
            )}
          </div>
        </aside>

        <section className="center-column">
          <div className="metrics-row">
            {/* The response at a glance: where the 13 units are, and what has happened to the people. */}
            <Metric icon={<Navigation size={14} />} label="ON THE WAY" value={pad(clock?.units?.en_route)} detail="units driving" />
            <Metric icon={<MapPin size={14} />} label="ON SCENE" value={pad(clock?.units?.busy)} detail="units working" />
            <Metric
              icon={<Check size={14} />}
              label="AVAILABLE"
              value={pad(clock?.units?.available)}
              detail={units.length
                ? `${units.filter((u) => u.status === "available" && !u.destination).length} at station · ${units.filter((u) => u.status === "available" && u.destination).length} in the field`
                : "units free to send"}
            />
            <Metric icon={<ArrowUpRight size={14} />} label="RESCUED" value={pad(clock?.rescued)} detail="people freed" />
            <Metric icon={<ShieldCheck size={14} />} label="TREATED" value={pad(clock?.treated)} detail="critical treated" />
            <Metric icon={<AlertTriangle size={14} />} label="DIED WAITING" value={pad(clock?.died_waiting)} detail="not reached in time" warning />
          </div>
          <section
            className="map-panel"
            aria-label="3D simulation map"
            ref={mapPanel}
          >
            <ShinjukuMap ref={mapRef} onUpdate={setMapState} />
            {/* Full screen hides the dashboard, so the same figures ride on the map as a see-through card. */}
            {fullscreen && clock?.run_id && (
              <div className="map-stats-overlay" aria-label="Response figures">
                <h3>Response</h3>
                <dl>
                  <div><dt>On the way</dt><dd>{clock.units?.en_route ?? 0}</dd></div>
                  <div><dt>On scene</dt><dd>{clock.units?.busy ?? 0}</dd></div>
                  <div><dt>Available</dt><dd>{clock.units?.available ?? 0}</dd></div>
                </dl>
                <h3>People</h3>
                <dl>
                  <div><dt>Rescued</dt><dd>{clock.rescued ?? 0}</dd></div>
                  <div><dt>Treated</dt><dd>{clock.treated ?? 0}</dd></div>
                  <div className="loss"><dt>Died waiting</dt><dd>{clock.died_waiting ?? 0}</dd></div>
                </dl>
              </div>
            )}
            <div className="map-toolbar">
              <button aria-label="Zoom in" onClick={() => mapRef.current?.zoomIn()}>
                <PlusIcon />
              </button>
              <button aria-label="Zoom out" onClick={() => mapRef.current?.zoomOut()}>
                <Minus size={15} />
              </button>
              <span />
              <button aria-label="Recentre on Shinjuku Station" onClick={() => mapRef.current?.recenter()}>
                <Crosshair size={15} />
              </button>
              <button
                aria-label={fullscreen ? "Leave full screen" : "Show the map full screen"}
                title={fullscreen ? "Leave full screen (Esc)" : "Full screen"}
                onClick={toggleFullscreen}
              >
                {fullscreen ? <X size={15} /> : <Maximize2 size={14} />}
              </button>
            </div>
            <div className="map-attribution">
              © OpenStreetMap contributors · OpenFreeMap · Buildings: Project PLATEAU (MLIT){" "}
              <span>35.69° N　139.70° E</span>
            </div>
          </section>
        </section>

        <aside className="right-column">
          <section className="panel incidents-panel">
            <div className="panel-heading">
              <div>
                <AlertTriangle size={14} />
                <span>INCIDENTS · PRIORITY</span>
              </div>
              <span className={pending.length ? "observed proposals-waiting" : "observed"}>
                {pending.length ? `${pending.length} TO APPROVE` : `${queue.length} OPEN`}
              </span>
            </div>
            <div className="incident-list">
              {queue.filter(({ incident }) => !dismissed.has(`incident:${incident.id}`))
                .map(({ incident, priority, trapped, critical, injured, waiting }, index) => {
                const proposals = pending.filter((d) => d.incident_id === incident.id);
                const jobs = jobsFor(incident.id);
                const allDone = jobs.length > 0 && !waiting && jobs.every((u) => u.status === "available");
                const onIt = units.filter((u) => u.destination?.incident_id === incident.id && u.status !== "available");
                const where = incident.building_id ? mapRef.current?.buildingName(incident.building_id) : null;
                const at = incident.location?.coordinates;
                return (
                  <div className={`incident-row priority-${priority}${waiting ? " has-proposal" : ""}`} key={incident.id}>
                    <button className="incident-main" onClick={() => at && mapRef.current?.focus(at[0], at[1])}>
                      <span className="incident-rank">{index + 1}</span>
                      <span className={`priority-badge priority-${priority}`}>{priority.toUpperCase()}</span>
                      <strong>
                        {[trapped && `${trapped} trapped`, critical && `${critical} critical`, injured && `${injured} injured`]
                          .filter(Boolean).join(" · ") || "Damage, no casualties known"}
                      </strong>
                      <small>
                        {where ?? "Location from reports"} · {incident.status} · {incident.evidence?.supports.length ?? 0} report
                        {(incident.evidence?.supports.length ?? 0) === 1 ? "" : "s"}
                        {onIt.length ? ` · ${onIt.map((u) => UNIT_LABEL[u.type] ?? u.type).join(", ")} on it` : ""}
                      </small>
                    </button>
                    <button className={`recon-link${recons[incident.id] ? " flown" : ""}`} title="Drone recon: see the block from above"
                      onClick={() => openRecon(incident.id)}>
                      <Plane size={11} /> {recons[incident.id] ? "Drone ✓" : "Drone"}
                    </button>
                    {jobs.length > 0 && (
                      <div className="incident-jobs">
                        {jobs.map((u) => (
                          <span key={u.id} className={`job job-${u.status}`}>
                            {UNIT_LETTER_LABEL[u.type] ?? "U"} {u.status === "en_route" ? "on the way"
                              : u.status === "busy" ? "on scene" : `done${u.helped ? ` · ${u.helped} helped` : ""}`}
                          </span>
                        ))}
                        {allDone && (
                          <button className="dismiss" title="Clear from the list" onClick={() => dismiss(`incident:${incident.id}`)}>
                            <X size={11} /> Units done
                          </button>
                        )}
                      </div>
                    )}
                    {proposals.map((d) => {
                      const unit = d.assignments?.[0]?.resource_id ?? "";
                      const state = acting[d.id];
                      return (
                        <div className="proposal" key={d.id}>
                          <span>
                            Agent proposes <b>{unitName(unit)}</b>
                            {d.eta_s != null ? ` · ETA ${Math.max(1, Math.round(d.eta_s / 60))} min` : ""}
                          </span>
                          {state && state !== "done" && state !== "sending" && state !== "rejecting"
                            ? <em className="proposal-error">{state}</em>
                            : (
                              <span className="proposal-actions">
                                <button className="approve" disabled={Boolean(state)} onClick={() => decide(d, incident, true)}>
                                  <Check size={12} /> {state === "sending" ? "Sending…" : "Approve"}
                                </button>
                                <button className="reject" disabled={Boolean(state)} onClick={() => decide(d, incident, false)}>
                                  <X size={12} /> Reject
                                </button>
                              </span>
                            )}
                        </div>
                      );
                    })}
                  </div>
                );
              })}
              {queue.length === 0 && (
                <div className="panel-empty">
                  {runId ? "No incidents yet. The agents build them from the reports as they come in." : "No run is playing."}
                </div>
              )}
            </div>
          </section>
          <section className="panel progress-panel activity-panel">
            <div className="panel-heading">
              <div>
                <Signal size={14} />
                <span>AGENT ACTIVITY</span>
              </div>
              {agentsLive
                ? <span className="mini-live"><i /> AGENTS LIVE</span>
                : <span className="observed">{runId ? (agentsStatus?.enabled ? "AGENTS STOPPED" : "AGENTS OFF") : "NO RUN"}</span>}
            </div>
            <div className="route-status-list activity-list">
              {activity.map((entry) => (
                <div className={`route-status-item activity-item activity-${entry.kind}`} key={entry.key}>
                  <div className="route-status-top">
                    <span>{entry.who}{entry.note ? ` · ${entry.note}` : ""}</span>
                    <b>T+{formatClock(entry.at)}</b>
                  </div>
                  <strong>{entry.title}</strong>
                  {entry.lines.map((line, index) => <small key={index}>{line}</small>)}
                </div>
              ))}
              {activity.length === 0 && (
                <div className="panel-empty">
                  {!runId
                    ? "No run is playing."
                    : agentsLive
                      ? "The agents are reading the first reports…"
                      : "Nothing from the agents yet. They start with each new earthquake when ./dev.sh is running."}
                </div>
              )}
            </div>
          </section>
          {driving.length > 0 && (
          <section className="panel progress-panel">
            <div className="panel-heading">
              <div>
                <Navigation size={14} />
                <span>ROUTE STATUS</span>
              </div>
              <span className="observed">{driving.length} ON THE WAY</span>
            </div>
            <div className="route-status-list">
              {driving.map((unit) => (
                <div className="route-status-item" key={unit.id}>
                  <div className="route-status-top">
                    <span>{UNIT_LABEL[unit.type] ?? unit.type}</span>
                    <b>{unit.decision?.reason ? "REROUTED" : "ACTIVE"}</b>
                  </div>
                  <strong>{unit.name} → {destinationName(unit)}</strong>
                  <small>
                    {unit.decision?.reason
                      ? `${unit.decision.reason}. Now on plan v${unit.decision.version}.`
                      : `On the way · plan v${unit.decision?.version ?? 1}`}
                  </small>
                </div>
              ))}
              {driving.length === 0 && (
                <div className="panel-empty">
                  {units.length ? "No unit is on the road right now." : "No run is playing."}
                </div>
              )}
            </div>
            <p className="canonical-note">
              {roadsRestricted} road segment{roadsRestricted === 1 ? "" : "s"} believed blocked or limited.
            </p>
          </section>
          )}
          <section className="panel dispatch-panel">
            <div className="dispatch-heading">
              <div className="panel-heading">
                <div>
                  <Navigation size={14} />
                  <span>RESCUE DISPATCH</span>
                </div>
                <span className="mini-live">
                  <i /> {units.length} UNITS
                </span>
              </div>
            </div>
            <p className="dispatch-instruction">
              Every responder unit as the simulator has it now. Click one to follow it on the map.
            </p>
            <div className="team-list unit-list">
              {units.map((unit) => (
                <button
                  className="team-row unit-row"
                  key={unit.id}
                  title={unit.name}
                  onClick={() => {
                    mapRef.current?.focus(unit.lon, unit.lat);
                    mapRef.current?.follow(unit.destination ? unit.id : null);   // ride along with a unit that has been sent
                  }}
                >
                  <span className="team-dot" style={{ background: UNIT_COLOR[unit.type] ?? UNIT_COLOR.police }} />
                  <strong>{unit.name}</strong>
                  <span className={unit.status === "available" ? "team-ready" : "team-enroute"}>{unitState(unit)}</span>
                  <span className="team-eta">{unit.decision ? `v${unit.decision.version}` : "—"}</span>
                  {unit.destination && unit.status === "available" && !dismissed.has(jobKey(unit)) && (
                    <div className="team-route team-done">
                      <span>DONE</span>
                      {destinationName(unit)}
                      <b>{unit.helped ? `${unit.helped} helped` : ""}</b>
                      <span className="dismiss" role="button" title="Clear this finished job"
                            onClick={(event) => { event.stopPropagation(); dismiss(jobKey(unit)); }}>
                        <X size={11} />
                      </span>
                    </div>
                  )}
                  {unit.destination && unit.status !== "available" && (
                    <div className="team-route">
                      <span>{unit.status === "busy" ? "AT" : "TO"}</span>
                      {destinationName(unit)}
                      <b>{unit.helped ? `${unit.helped} helped` : ""}</b>
                    </div>
                  )}
                </button>
              ))}
              {units.length === 0 && (
                <div className="panel-empty">No units yet. They appear when an earthquake is generated.</div>
              )}
            </div>
          </section>
        </aside>
      </main>
      {reportOpen && selectedReport && (
        <div
          className="report-modal-backdrop"
          onMouseDown={(event) => {
            if (event.target === event.currentTarget) setReportOpen(false);
          }}
        >
          <section
            className="report-modal"
            role="dialog"
            aria-modal="true"
            aria-labelledby="report-modal-title"
          >
            <div className="report-modal-header">
              <div>
                <span className="report-modal-kicker">{selectedReport.kind} report</span>
                <h2 id="report-modal-title">Report {selectedReport.id}</h2>
              </div>
              <button
                className="report-modal-close"
                aria-label="Close report"
                onClick={() => setReportOpen(false)}
              >
                <X size={17} />
              </button>
            </div>
            <div className="report-modal-meta">
              <span><Clock3 size={13} /> {selectedReport.time}{selectedReport.time.startsWith("T+") ? " after the quake" : " UTC"}</span>
              <span><MapPin size={13} /> {selectedReport.location}</span>
              <span className={`tiny-state ${selectedReport.status.toLowerCase()}`}>
                {selectedReport.status}
              </span>
            </div>
            <blockquote className="report-full-text">“{selectedReport.text}”</blockquote>
            {selectedReport.originalText && (
              <blockquote className="report-full-text" lang={selectedReport.lang}>
                <small>Original ({selectedReport.lang?.toUpperCase()}):</small> {selectedReport.originalText}
              </blockquote>
            )}
            <div className="report-modal-incident">
              <span>Linked incident</span>
              <strong>
                {linked
                  ? `${against ? "Speaks against an incident" : "Part of an incident"} (${linked.status})`
                  : "No incident cites this report yet"}
              </strong>
              <small>
                {linked
                  ? `Believed there: ${believedPeople(linked)}. Based on ${linked.evidence?.supports.length ?? 0} report${linked.evidence?.supports.length === 1 ? "" : "s"}.`
                  : "The assessment agents link reports to incidents as they work through them."}
              </small>
            </div>
            <div className="report-modal-footer">
              <span>
                {selectedReport.reliability !== undefined
                  ? `Source reliability ${selectedReport.reliability.toFixed(2)}`
                  : selectedReport.verified ? "Source marked verified" : "Awaiting source confirmation"}
              </span>
              <button onClick={() => setReportOpen(false)}>Done</button>
            </div>
          </section>
        </div>
      )}
      </div>
      {page === "analytics" ? <Suspense fallback={<div className="analytics-loading" role="status">Opening analytics…</div>}><AnalyticsPage /></Suspense> : null}
      <footer className="app-footer">
        <div>
          <Waves size={13} />
          <strong>KYU-BOT</strong>
          <span>SIMULATION ENVIRONMENT</span>
        </div>
        <span>DEMO SCENARIO · NOT FOR OPERATIONAL USE</span>
      </footer>
    </div>
  );
}

const UNIT_LETTER_LABEL: Record<string, string> = { fire_engine: "Engine", ambulance: "Ambulance", police: "Patrol" };

function KyuBotMark() {
  return (
    <svg
      aria-label="KYU-BOT logo"
      className="kyu-bot-mark"
      role="img"
      viewBox="0 0 64 64"
      xmlns="http://www.w3.org/2000/svg"
    >
      <g
        fill="none"
        stroke="#e4002b"
        strokeLinecap="round"
        strokeLinejoin="round"
        strokeWidth="4.2"
      >
        <path d="M20 17v30M20 32h13M33 32l16-17M33 32l16 17" />
        <circle cx="51" cy="14" r="4" />
        <circle cx="51" cy="50" r="4" />
      </g>
      <path d="m33 24 8 8-8 8-8-8z" fill="#171b22" />
      <path d="m33 28 4 4-4 4-4-4z" fill="#fff8f8" />
      <circle cx="33" cy="32" r="1.6" fill="#e4002b" />
    </svg>
  );
}

function Metric({
  icon,
  label,
  value,
  detail,
  warning = false,
}: {
  icon: React.ReactNode;
  label: string;
  value: string;
  detail: string;
  warning?: boolean;
}) {
  return (
    <div className="metric-card">
      <div className="metric-label">
        {icon}
        {label}
      </div>
      <div className={`metric-value ${warning ? "warning-value" : ""}`}>
        {value}
        <span>{detail}</span>
      </div>
    </div>
  );
}
function PlusIcon() {
  return <span className="plus-glyph">+</span>;
}
function MapIllustration({ zoom }: { zoom: number }) {
  return (
    <div className="map-art" style={{ transform: `scale(${zoom})` }}>
      <div className="sea" />
      <div className="coast" />
      <div className="land">
        <div className="terrain-lines" />
        <div className="neighborhood n1" />
        <div className="neighborhood n2" />
        <div className="neighborhood n3" />
        <div className="neighborhood n4" />
        <div className="green-patch p1" />
        <div className="green-patch p2" />
        <svg
          className="roads-svg"
          viewBox="0 0 900 520"
          preserveAspectRatio="none"
        >
          <path d="M-20 310 C130 255 180 330 285 270 S465 190 580 242 745 215 930 145" />
          <path d="M35 410 C170 350 240 355 345 300 S495 345 600 280 790 330 920 275" />
          <path d="M120 0 C180 120 155 180 240 260 S300 405 370 530" />
          <path d="M410 -20 C380 75 435 125 420 205 S505 385 480 550" />
          <path d="M680 -10 C635 95 690 155 620 250 S710 370 655 525" />
          <path d="M0 175 C160 210 240 155 365 195 S595 150 900 90" />
        </svg>
        {[
          [43, 45],
          [50, 48],
          [54, 51],
          [61, 48],
          [47, 56],
          [57, 59],
          [64, 43],
          [69, 52],
          [39, 61],
        ].map(([left, top], index) => (
          <div
            className={`building ${index < 4 ? "affected" : ""}`}
            style={{ left: `${left}%`, top: `${top}%` }}
            key={`${left}-${top}`}
          />
        ))}
        <div className="route-line" />
        <div className="map-pin pin-school">
          <MapPin size={19} fill="currentColor" />
          <span>W1 · COLEGIO</span>
        </div>
        <div className="map-pin pin-team">
          <MapPin size={19} fill="currentColor" />
          <span>RESCUE FIRE DEPARTMENT</span>
        </div>
        <div className="map-label label-main">AVENIDA PRINCIPAL DE CARIBE</div>
        <div className="map-label label-old">OLD PLAZA</div>
        <div className="map-label label-hospital">R. SAN JOSÉ</div>
        <div className="map-label label-city">Caraballeda</div>
        <div className="map-label label-north">PLAYA LOS COCOS</div>
      </div>
    </div>
  );
}
export default App;
