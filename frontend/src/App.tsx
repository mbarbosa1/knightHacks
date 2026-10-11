import { useEffect, useMemo, useRef, useState } from "react";
import {
  AlertTriangle,
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
import type { Decision, LiveUnit, OperationalState, ReconResult, Report } from "./map/dataSource";
import { ReconPage } from "./Recon";
import { approveProposal, incidentPriority, rejectProposal } from "./map/dataSource";

const DBX = "https://dbc-87980e9b-e483.cloud.databricks.com", DBX_ORG = "7474645633187109";
const EMBED_URL = `${DBX}/embed/dashboardsv3/01f1c57b716915d8b00465003e793b28?o=${DBX_ORG}`;
const DASHBOARD_URL = `${DBX}/dashboardsv3/01f1c57b716915d8b00465003e793b28/published?o=${DBX_ORG}`;
const GENIE_URL = `${DBX}/genie/rooms/01f1c580620a1d308658e770ef4e127c?o=${DBX_ORG}`;
type BelievedIncident = OperationalState["incidents"][number];

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
  const [page, setPage] = useState<"ops" | "recon" | "analytics">("ops");
  const [reconFor, setReconFor] = useState<string | null>(null);
  const [recons, setRecons] = useState<Record<string, ReconResult>>({});
  const openRecon = (incidentId: string) => { setReconFor(incidentId); setPage("recon"); };
  const destinationName = (unit: LiveUnit) =>
    unit.destination
      ? (unit.destination.building_id && mapRef.current?.buildingName(unit.destination.building_id)) ||
        `${unit.destination.lat.toFixed(4)}° N, ${unit.destination.lon.toFixed(4)}° E`
      : "";
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
          <span className="eyebrow">{page === "ops" ? "OPERATIONS OVERVIEW" : page === "recon" ? "DRONE RECON" : "ANALYTICS · DATABRICKS"}</span>
          <span className="scenario-name">Tokyo earthquake scenario</span>
        </div>
        <nav className="page-tabs">
          <button className={page === "ops" ? "active" : ""} onClick={() => setPage("ops")}><MapPin size={13} /> Operations</button>
          <button className={page === "recon" ? "active" : ""} onClick={() => setPage("recon")}>
            <Plane size={13} /> Drone recon{Object.keys(recons).length ? <small>{Object.keys(recons).length}</small> : null}
          </button>
          <button className={page === "analytics" ? "active" : ""} onClick={() => setPage("analytics")}>
            <Layers size={13} /> Analytics
          </button>
        </nav>
        <div className="top-actions">
          <span className="live-badge">
            <i /> SIMULATION
          </span>
          <span className="clock">
            {clock?.sim_time_s !== undefined
              ? <>T+{formatClock(clock.sim_time_s)} <small>SIM</small></>
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
      <div className="status-strip">
        <div>
          <span className="status-dot" /> {runState}{" "}
          <span className="strip-divider">/</span>{" "}
          {truth
            ? `M ${truth.scenario.mw.toFixed(1)} earthquake · Intensity ${truth.intensity.label} (Japan scale)`
            : "Generate an earthquake on the map to start"}
        </div>
        {clock?.run_id && (
          <div className="strip-stats">
            {clock.units && (
              <span>
                <b>{clock.units.en_route}</b> on the way · <b>{clock.units.busy}</b> on scene · <b>{clock.units.available}</b> free
              </span>
            )}
            <span><b>{clock.rescued ?? 0}</b> rescued</span>
            <span><b>{clock.treated ?? 0}</b> treated</span>
            <span className={clock.died_waiting ? "strip-loss" : ""}><b>{clock.died_waiting ?? 0}</b> died waiting</span>
          </div>
        )}
      </div>
      {page === "analytics" && (
        <main className="analytics-page">
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
            <Metric
              icon={<MapPin size={14} />}
              label="MAPPED LOCATIONS"
              value={observations ? String(observations.filter((o) => o.longitude != null).length).padStart(2, "0") : "00"}
              detail="Located reports"
            />
            <Metric
              icon={<AlertTriangle size={14} />}
              label="NEEDS LOCATION"
              value={observations ? String(observations.filter((o) => o.longitude == null).length).padStart(2, "0") : "00"}
              detail="Review reports"
              warning
            />
          </div>
          <section
            className="map-panel"
            aria-label="3D simulation map"
          >
            <ShinjukuMap ref={mapRef} onUpdate={setMapState} />
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
              Routes use the roads believed open; a unit that meets a blocked road reports it and takes another way.
            </p>
          </section>
          <section className="panel details-panel">
            <div className="panel-heading">
              <div>
                <Layers size={14} />
                <span>REPORT DETAILS</span>
              </div>
            </div>
            {selectedReport ? (
              <>
                <div className="detail-source">
                  <span>
                    <Radio size={12} />{" "}
                    {(CHANNELS[selectedReport.channel ?? ""]?.[0] ?? `${selectedReport.kind} report`).toUpperCase()}
                  </span>
                  <span className="unverified">{reportState}</span>
                </div>
                <h2>{selectedReport.id} · {selectedReport.time}</h2>
                <p className="detail-text">{selectedReport.text}</p>
                <p className="detail-address">
                  <MapPin size={12} /> {selectedReport.location}
                </p>
                <div className="detail-tags">
                  {selectedReport.precision && <span>Location: {selectedReport.precision}</span>}
                  {selectedReport.reliability !== undefined && (
                    <span>Source reliability {selectedReport.reliability.toFixed(2)}</span>
                  )}
                  {selectedReport.originalText && <span>Sent in {selectedReport.lang?.toUpperCase()}</span>}
                  {selectedReport.longitude === undefined && obsId && <span>Not on the map</span>}
                </div>
                <div className="verification">
                  <ShieldCheck size={14} />
                  <span>
                    {linked
                      ? `${against ? "Speaks against an incident" : "Part of an incident"}: ${believedPeople(linked)}`
                      : "No incident cites this report yet"}
                  </span>
                </div>
                <button
                  className="open-report-button"
                  onClick={() => setReportOpen(true)}
                >
                  <ExternalLink size={13} /> Open full report
                  <ChevronRight size={13} />
                </button>
              </>
            ) : (
              <div className="panel-empty">Select a report in the feed to see it here and find it on the map.</div>
            )}
          </section>
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
              Every responder unit as the simulator has it now. Click one to find it on the map.
            </p>
            <div className="team-list unit-list">
              {units.map((unit) => (
                <button
                  className="team-row unit-row"
                  key={unit.id}
                  title={unit.name}
                  onClick={() => mapRef.current?.focus(unit.lon, unit.lat)}
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
