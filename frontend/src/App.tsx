import { useEffect, useMemo, useRef, useState } from "react";
import {
  AlertTriangle,
  Antenna,
  ArrowDownRight,
  ArrowUpRight,
  Check,
  CheckCircle2,
  ChevronDown,
  ChevronRight,
  CircleHelp,
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
  Play,
  Radio,
  RotateCcw,
  Search,
  Send,
  Settings2,
  ShieldCheck,
  Signal,
  SlidersHorizontal,
  Square,
  Users,
  Volume2,
  Waves,
  X,
  ZoomIn,
  ZoomOut,
} from "lucide-react";
import { mockReports, type MockReport, type ReportKind } from "./data/mockReports";
import { ShinjukuMap, type ShinjukuMapHandle } from "./map/ShinjukuMap";
import type { MapState } from "./map/ShinjukuMap";
import type { Report } from "./map/dataSource";

interface Incident {
  id: string;
  title: string;
  address: string;
  severity: "Critical" | "High" | "Moderate";
  tags: string[];
  verified: boolean;
}
interface Dispatch {
  id: string;
  team: string;
  state: "En route" | "Available";
  destination: string;
  eta: string;
  color: string;
}

const incidents: Incident[] = [
  {
    id: "INC-042",
    title: "Structural damage · trapped victims · access blocked",
    address: "Shinjuku Mayor's Office, Av. José María España",
    severity: "Critical",
    tags: ["Structural damage", "Trapped victims", "Access blocked"],
    verified: true,
  },
  {
    id: "INC-038",
    title: "Partial collapse · residents need assistance",
    address: "Residencias Caribe, Avenida Principal",
    severity: "High",
    tags: ["Partial collapse", "Medical support"],
    verified: true,
  },
  {
    id: "INC-031",
    title: "Road obstruction reported near hillside",
    address: "Calle Sucre, Caraballeda",
    severity: "Moderate",
    tags: ["Road blocked", "Debris"],
    verified: false,
  },
];

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
  };
}
const dispatches: Dispatch[] = [
  {
    id: "D-01",
    team: "Rescue Fire Department",
    state: "En route",
    destination: "Residencias Caribe",
    eta: "0:16",
    color: "cyan",
  },
  {
    id: "D-02",
    team: "Rescue Medical Team",
    state: "Available",
    destination: "Colegio Tanagueranas",
    eta: "—",
    color: "violet",
  },
  {
    id: "D-03",
    team: "Rescue Police Department",
    state: "Available",
    destination: "Standby · Sector 4",
    eta: "—",
    color: "green",
  },
];
const routeUpdates = [
  { label: "Current route", title: "Rescue Fire Department → Residencias Caribe", meta: "En route · ETA 16 min", status: "ACTIVE" },
  { label: "Expected next", title: "Recalculate after road status update", meta: "Uses live unit position and road conditions", status: "NEXT" },
  { label: "Fallback route", title: "Alternate path retained", meta: "Ready if the primary route is blocked", status: "READY" },
];

function App() {
  const [filter, setFilter] = useState<"All" | ReportKind>("All");
  const [search, setSearch] = useState("");
  const [selected, setSelected] = useState("INC-042");
  const [selectedReportId, setSelectedReportId] = useState("R-1042");
  const [reportOpen, setReportOpen] = useState(false);
  const [playing, setPlaying] = useState(false);
  const [dispatched, setDispatched] = useState(false);
  const [utc, setUtc] = useState(new Date());
  const mapRef = useRef<ShinjukuMapHandle>(null);
  // The map follows the saved run and collects its reports as the live clock releases them into Neon
  // (Gemini-worded when the simulator runs with SIM_GEMINI=1). The feed shows the same list, so it fills in one
  // by one. With no saved run on the map yet, the sample reports.
  const [mapState, setMapState] = useState<MapState | null>(null);
  const observations: Report[] | null = mapState?.truth?.ingestion?.ready ? mapState.reports : null;
  const reports: MockReport[] = useMemo(
    () => observations
      ? [...observations].sort((a, b) => (b.sim_time_s ?? 0) - (a.sim_time_s ?? 0)).map(toReport)   // newest first
      : mockReports,
    [observations],
  );
  const incident =
    incidents.find((item) => item.id === selected) ?? incidents[0];
  const selectedReport: MockReport =
    reports.find((report) => report.id === selectedReportId) ?? reports[0];
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
          <span className="eyebrow">OPERATIONS OVERVIEW</span>
          <span className="scenario-name">Tokyo earthquake scenario</span>
        </div>
        <div className="top-actions">
          <span className="live-badge">
            <i /> SIMULATION
          </span>
          <span className="clock">
            {utc.toISOString().slice(11, 19)} <small>UTC</small>
          </span>
          <button className="session-button">
            <Users size={14} /> Session <ChevronDown size={13} />
          </button>
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
          <span className="status-dot" /> DEMO PAUSED{" "}
          <span className="strip-divider">/</span> TOKYO command
        </div>
        <div className="demo-controls">
          <button
            onClick={() => {
              setSelected("INC-042");
              setFilter("All");
              setSearch("");
              setDispatched(false);
              setPlaying(false);
            }}
          >
            <RotateCcw size={13} /> Reset demo
          </button>
          <button onClick={() => setPlaying(!playing)}>
            {playing ? <Square size={12} /> : <Play size={12} />}{" "}
            {playing ? "Pause" : "Resume"}
          </button>
          <button>
            <Play size={12} /> Start Caraballeda demo <ChevronRight size={12} />
          </button>
          <button>
            <SlidersHorizontal size={13} /> Sources & replay{" "}
            <ChevronRight size={12} />
          </button>
        </div>
      </div>
      <main className="dashboard">
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
          <div className="feed-options">
            <label>
              <input type="checkbox" /> Show filtered
            </label>
            <span>
              NEWEST FIRST <ChevronDown size={11} />
            </span>
          </div>
          <div className="report-list">
            {visibleReports.map((report) => (
              <button
                key={report.id}
                className={`report-card ${(report.incidentId ? selected === report.incidentId : selectedReportId === report.id) ? "selected" : ""} ${report.kind.toLowerCase()}`}
                onClick={() => {
                  if (report.incidentId) setSelected(report.incidentId);   // real reports have no incident yet
                  setSelectedReportId(report.id);
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
                  <span className="replay">REPLAY</span>
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
                {reports.length === 0
                  ? "No reports yet. They arrive as the simulation clock runs."
                  : "No reports match this search."}
              </div>
            )}
          </div>
          <button className="sources-button">
            <SlidersHorizontal size={13} /> Sources & replay tools{" "}
            <ChevronRight size={13} />
          </button>
        </aside>

        <section className="center-column">
          <div className="metrics-row">
            <Metric
              icon={<Antenna size={14} />}
              label="INCOMING REPORTS"
              value={observations ? String(observations.length).padStart(2, "0") : "04"}
              detail="Radio + external"
            />
            <Metric
              icon={<MapPin size={14} />}
              label="MAPPED LOCATIONS"
              value={observations ? String(observations.filter((o) => o.longitude != null).length).padStart(2, "0") : "02"}
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
          <section className="panel progress-panel">
            <div className="panel-heading">
              <div>
                <Navigation size={14} />
                <span>ROUTE STATUS</span>
              </div>
              <span className="observed">LIVE</span>
            </div>
            <div className="route-status-list">
              {routeUpdates.map((route) => (
                <div className="route-status-item" key={route.label}>
                  <div className="route-status-top">
                    <span>{route.label}</span>
                    <b>{route.status}</b>
                  </div>
                  <strong>{route.title}</strong>
                  <small>{route.meta}</small>
                </div>
              ))}
            </div>
            <p className="canonical-note">
              Route plans use the current road state and may change when conditions update.
            </p>
          </section>
          <section className="panel details-panel">
            <div className="panel-heading">
              <div>
                <Layers size={14} />
                <span>REPORT DETAILS</span>
              </div>
              <CircleHelp size={14} className="muted-icon" />
            </div>
            <div className="detail-source">
              <span>
                <Radio size={12} /> {reports.find((report) => report.incidentId === incident.id)?.kind.toUpperCase() ?? "INCIDENT REPORT"}
              </span>
              <span className="unverified">
                {incident.verified ? "VERIFIED" : "UNVERIFIED"}
              </span>
            </div>
            <h2>{incident.title}</h2>
            <p className="detail-address">
              <MapPin size={12} /> {incident.address}
            </p>
            <div className="detail-tags">
              {incident.tags.map((tag) => (
                <span key={tag}>{tag}</span>
              ))}
            </div>
            <div className="verification">
              <ShieldCheck size={14} />
              <span>
                {incident.verified
                  ? "Cross-verified by 2 sources"
                  : "Awaiting source confirmation"}
              </span>
              <ChevronRight size={13} />
            </div>
            <button
              className="open-report-button"
              onClick={() => setReportOpen(true)}
            >
              <ExternalLink size={13} /> Open full report
              <ChevronRight size={13} />
            </button>
          </section>
          <section className="panel dispatch-panel">
            <div className="dispatch-heading">
              <div className="panel-heading">
                <div>
                  <Navigation size={14} />
                  <span>RESCUE DISPATCH</span>
                </div>
                <span className="mini-live">
                  <i /> LIVE
                </span>
              </div>
            </div>
            <p className="dispatch-instruction">
              Click a road to mark it BLOCKED. Save replans from the live unit
              position against canonical road state. Some objectives may no
              longer be reachable.
            </p>
            <div className="team-list">
              {dispatches.map((team) => (
                <div className="team-row" key={team.id}>
                  <span className={`team-dot ${team.color}`} />
                  <strong>{team.team}</strong>
                  <span
                    className={
                      team.state === "En route" ? "team-enroute" : "team-ready"
                    }
                  >
                    {team.state}
                  </span>
                  <span className="team-eta">{team.eta}</span>
                  <div className="team-route">
                    <span>
                      {team.id === "D-01"
                        ? "W1"
                        : team.id === "D-02"
                          ? "W7"
                          : "STANDBY"}
                    </span>
                    {team.destination}
                    <b>
                      {team.id === "D-01"
                        ? "0.74"
                        : team.id === "D-02"
                          ? "0.62"
                          : "—"}
                    </b>
                  </div>
                </div>
              ))}
            </div>
            <button
              className={`dispatch-button ${dispatched ? "saved" : ""}`}
              onClick={() => setDispatched(!dispatched)}
            >
              {dispatched ? <CheckCircle2 size={14} /> : <Send size={13} />}
              {dispatched
                ? "Dispatch saved · Route updated"
                : "Save dispatch & replan"}
              <ChevronRight size={13} />
            </button>
          </section>
        </aside>
      </main>
      {reportOpen && (
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
              <strong>{incident.id} · {incident.title}</strong>
              <small>{incident.address}</small>
            </div>
            <div className="report-modal-footer">
              <span>{selectedReport.verified ? "Source marked verified" : "Awaiting source confirmation"}</span>
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
        <button>
          <CircleHelp size={13} /> Help & documentation
        </button>
      </footer>
    </div>
  );
}

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
      <ChevronRight size={13} className="metric-arrow" />
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
