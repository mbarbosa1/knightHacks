import { useEffect, useMemo, useState } from "react";
import {
  Activity,
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
  Layers,
  LocateFixed,
  Map,
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
  Wind,
  X,
  ZoomIn,
  ZoomOut,
} from "lucide-react";

type ReportKind = "Social" | "Drone" | "Radio";
type ReportStatus = "Processed" | "Replay";
interface Incident {
  id: string;
  title: string;
  address: string;
  severity: "Critical" | "High" | "Moderate";
  tags: string[];
  verified: boolean;
}
interface Report {
  id: string;
  kind: ReportKind;
  time: string;
  status: ReportStatus;
  text: string;
  location: string;
  verified: boolean;
  incidentId: string;
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

// Mock API-shaped data: these models can be mapped directly to FastAPI / Neon records.
const reports: Report[] = [
  {
    id: "R-1042",
    kind: "Social",
    time: "10:03",
    status: "Processed",
    text: "“Someone said children may be trapped at Shinjuku Mayor's Office. I didn’t see it myself.”",
    location: "Colegio Tanagueranas",
    verified: true,
    incidentId: "INC-042",
  },
  {
    id: "R-1041",
    kind: "Drone",
    time: "10:02",
    status: "Processed",
    text: "“Drone scan: confirmed multiple. Reported count: 6. Structure: severe collapse. Access: limited.”",
    location: "Residencias Caribe A",
    verified: true,
    incidentId: "INC-038",
  },
  {
    id: "R-1040",
    kind: "Radio",
    time: "10:00",
    status: "Replay",
    text: "“Command, Rescue Team Alpha: partial collapse at Residencias Caribe. Avenida Principal is blocked.”",
    location: "Avenida Principal",
    verified: false,
    incidentId: "INC-038",
  },
  {
    id: "R-1039",
    kind: "Social",
    time: "09:58",
    status: "Processed",
    text: "“Road is clear up to the plaza. Debris is covering the north entrance.”",
    location: "Plaza Bolívar",
    verified: false,
    incidentId: "INC-031",
  },
];
const dispatches: Dispatch[] = [
  {
    id: "D-01",
    team: "Rescue Team Alpha",
    state: "En route",
    destination: "Residencias Caribe",
    eta: "0:16",
    color: "cyan",
  },
  {
    id: "D-02",
    team: "Rescue Team Bravo",
    state: "Available",
    destination: "Colegio Tanagueranas",
    eta: "—",
    color: "violet",
  },
  {
    id: "D-03",
    team: "Rescue Team Charlie",
    state: "Available",
    destination: "Standby · Sector 4",
    eta: "—",
    color: "green",
  },
];
const eventSteps = [
  "Social report arrives · building turns yellow",
  "Belief opens an uncertain worksite · Search launches a drone",
  "Radio corroborates · Save dispatches a rescue team",
  "Later social reports add more yellow targets",
  "Drone scans resolve buildings red or green",
  "Rumoured site cleared by a negative scan",
  "Limited road segments show yellow",
];

function App() {
  const [filter, setFilter] = useState<"All" | ReportKind>("All");
  const [search, setSearch] = useState("");
  const [selected, setSelected] = useState("INC-042");
  const [playing, setPlaying] = useState(false);
  const [terrain, setTerrain] = useState(true);
  const [roads, setRoads] = useState(false);
  const [dispatched, setDispatched] = useState(false);
  const [utc, setUtc] = useState(new Date());
  const [zoom, setZoom] = useState(1);
  const incident =
    incidents.find((item) => item.id === selected) ?? incidents[0];
  const visibleReports = useMemo(
    () =>
      reports.filter(
        (report) =>
          (filter === "All" || report.kind === filter) &&
          `${report.text} ${report.location} ${report.kind}`
            .toLowerCase()
            .includes(search.toLowerCase()),
      ),
    [filter, search],
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
            {(["All", "Radio", "Social", "Drone"] as const).map((item) => (
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
                className={`report-card ${selected === report.incidentId ? "selected" : ""} ${report.kind.toLowerCase()}`}
                onClick={() => setSelected(report.incidentId)}
              >
                <div className="report-top">
                  <div className="report-type">
                    {report.kind === "Social" ? (
                      <Square size={13} />
                    ) : report.kind === "Drone" ? (
                      <Wind size={14} />
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
              <div className="empty-state">No reports match this search.</div>
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
              value="04"
              detail="Radio + external"
            />
            <Metric
              icon={<MapPin size={14} />}
              label="MAPPED LOCATIONS"
              value="02"
              detail="Located reports"
            />
            <Metric
              icon={<AlertTriangle size={14} />}
              label="NEEDS LOCATION"
              value="00"
              detail="Review reports"
              warning
            />
          </div>
          <section
            className="map-panel"
            aria-label="3D simulation map placeholder"
          >
            {/* TODO: Mount a CesiumJS or MapLibre canvas in this map stage when map data is connected. */}
            <MapIllustration zoom={zoom} />
            <div className="map-header">
              <div className="map-place">
                <MapPin size={15} />
                <div>
                  <strong>Tokyo, Japan</strong>
                  <small>SHINJUKU · SATELLITE · TERRAIN</small>
                </div>
              </div>
              <span className="map-3d">3D</span>
            </div>
            <div className="map-layer-card">
              <div className="layer-title">
                Map layers{" "}
                <button aria-label="Layer settings">
                  <Settings2 size={13} />
                </button>
              </div>
              <label>
                <input
                  type="checkbox"
                  checked={terrain}
                  onChange={(event) => setTerrain(event.target.checked)}
                />{" "}
                <span>Terrain elevation</span>
              </label>
              <label>
                <input
                  type="checkbox"
                  checked={roads}
                  onChange={(event) => setRoads(event.target.checked)}
                />{" "}
                <span>Road graph</span>
              </label>
              <button className="geography-button">
                <LocateFixed size={12} /> Use local geography
              </button>
            </div>
            <div className="map-toolbar">
              <button
                aria-label="Zoom in"
                onClick={() => setZoom((value) => Math.min(1.4, value + 0.1))}
              >
                <PlusIcon />
              </button>
              <button
                aria-label="Zoom out"
                onClick={() => setZoom((value) => Math.max(0.8, value - 0.1))}
              >
                <Minus size={15} />
              </button>
              <span />
              <button aria-label="Locate">
                <Crosshair size={15} />
              </button>
              <button aria-label="Navigation">
                <Navigation size={15} />
              </button>
              <button aria-label="Terrain">
                <Map size={15} />
              </button>
              <button aria-label="Layers">
                <Layers size={15} />
              </button>
            </div>
            <div className="map-attribution">
              © OpenStreetMap contributors · GeoFabrik · ODbL{" "}
              <span>10.61° N　66.84° W</span>
            </div>
            <div className="map-legend">
              <span>
                <i className="legend-critical" /> Critical
              </span>
              <span>
                <i className="legend-reported" /> Reported
              </span>
              <span>
                <i className="legend-clear" /> Cleared
              </span>
            </div>
          </section>
          <section className="drone-panel panel">
            <div className="drone-header">
              <div className="panel-heading">
                <div>
                  <Wind size={14} />
                  <span>DRONE PROGRESS</span>
                </div>
                <CircleHelp size={14} className="muted-icon" />
              </div>
              <span className="mini-live">
                <i /> LIVE
              </span>
            </div>
            <div className="drone-units">
              <div className="unit">
                <div className="unit-heading">
                  <strong>Drone Unit 1</strong>
                  <span className="state-enroute">En route</span>
                </div>
                <span className="unit-destination">Shinjuku Mayor's Office</span>
                <div className="progress-track">
                  <i />
                </div>
                <div className="unit-foot">
                  <span>ETA 1:37</span>
                  <span>72% battery</span>
                </div>
              </div>
              <div className="unit">
                <div className="unit-heading">
                  <strong>Drone Unit 2</strong>
                  <span className="state-ready">Ready to fly</span>
                </div>
                <span className="unit-destination">
                  Available for a search task
                </span>
                <div className="progress-track idle">
                  <i />
                </div>
                <div className="unit-foot">
                  <span>Ready to fly</span>
                  <span>94% battery</span>
                </div>
              </div>
            </div>
            <div className="drone-footer">
              <span className="flying">
                <i /> FLYING
              </span>
              <span>
                1 drone mission running · 2 worksites in canonical belief.
              </span>
              <button>
                View mission <ChevronRight size={12} />
              </button>
            </div>
          </section>
        </section>

        <aside className="right-column">
          <section className="panel progress-panel">
            <div className="panel-heading">
              <div>
                <Activity size={14} />
                <span>DEMO PROGRESS</span>
              </div>
              <span className="observed">6/8 OBSERVED</span>
            </div>
            <ol className="timeline">
              {eventSteps.map((step, index) => (
                <li
                  key={step}
                  className={index < 3 ? "done" : index === 3 ? "current" : ""}
                >
                  <span className="timeline-dot" />
                  <span>
                    {index + 1}. {step}
                  </span>
                </li>
              ))}
            </ol>
            <div className="map-summary">
              <strong>BUILDINGS</strong>
              <span>
                <i className="orange" /> 1 reported
              </span>
              <span>
                <i className="red" /> 1 people
              </span>
              <span>
                <i className="green" /> 0 clear
              </span>
            </div>
            <div className="map-summary roads-summary">
              <strong>ROADS</strong>
              <span>
                <i className="yellow" /> 2 limited
              </span>
              <span>
                <i className="red" /> 1 blocked
              </span>
            </div>
            <p className="canonical-note">
              Canonical belief state version 9 · colours follow drone findings,
              not a safety clearance.
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
                <Wind size={12} /> DRONE REPORT
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
          <span>RESCUE TEAM ALPHA</span>
        </div>
        <div className="drone-cluster">
          ✣<i>✣</i>
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
