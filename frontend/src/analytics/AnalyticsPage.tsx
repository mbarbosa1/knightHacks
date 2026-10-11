import { useEffect, useMemo, useState } from "react";
import { BarChart3, Database, Download, ExternalLink, Pause, Play, RefreshCw, Search, Sparkles, Send } from "lucide-react";
import ReplayMap from "./ReplayMap";
import { askGenie, clock, date, download, eventTime, label, number, object, read, snapshot, text, timestamp, type Bundle, type Connection, type Row, type Scenario, type GenieReply } from "./data";
import "./analytics.css";

const TABS = ["Overview", "Replay", "Data explorer", "Performance"] as const;
type Tab = typeof TABS[number];
const EMPTY: Row[] = [];
// Counts supplied by the team in chat; these are a snapshot, never a live query.
const SHARED_SNAPSHOT = [
  ["truth.scenarios", "19", "Earthquake scenarios"],
  ["history.simulation_events", "711", "Exported reports and agent actions"],
  ["eval.scorecards", "10", "Evaluation metrics from 1 scored run"],
  ["truth.truth_building_damage", "≈101,000", "Building damage states"],
  ["truth.truth_people", "≈94,000", "People and condition states"],
  ["truth.truth_road_status", "694", "Road status records"],
  ["truth.truth_links", "4,789", "Report and truth classifications"],
];
const METRICS: Record<string, [string, boolean]> = {
  rescue_found_rate: ["Rescue buildings detected", true], rescue_people_found: ["People in reported need detected", true],
  incident_precision: ["Incident precision", true], rumors_caught: ["Rumor reports not acted on", true],
  road_block_recall: ["Road restriction recall", true], road_block_precision: ["Road restriction precision", true],
  rumors_acted_on: ["Incidents based only on rumors", false], duplicate_incidents: ["Duplicate incidents", false],
  people_helped_by_units: ["People rescued or treated", false], died_waiting: ["People who died waiting", false],
};
export default function AnalyticsPage() {
  const [connection, setConnection] = useState<Connection | null>(null);
  const [runs, setRuns] = useState<Scenario[]>([]);
  const [runId, setRunId] = useState("");
  const [bundle, setBundle] = useState<Bundle | null>(null);
  const [tab, setTab] = useState<Tab>("Overview");
  const [error, setError] = useState("");
  const [loading, setLoading] = useState(true);
  const [revision, setRevision] = useState(0);
  const [at, setAt] = useState<number | null>(null);
  const [playing, setPlaying] = useState(false);
  const [speed, setSpeed] = useState(5);
  const [focused, setFocused] = useState<Row | null>(null);
  const [year, setYear] = useState("all");
  const [eventCategory, setEventCategory] = useState("all");
  const [genieQuestion, setGenieQuestion] = useState("");
  const [genieReply, setGenieReply] = useState<GenieReply | null>(null);
  const [genieBusy, setGenieBusy] = useState(false);
  const [genieError, setGenieError] = useState("");
  useEffect(() => {
    const controller = new AbortController();
    setLoading(true); setError("");
    (async () => {
      const status = await read<Connection>("/status", controller.signal);
      setConnection(status);
      if (!status.configured) { setRuns([]); setBundle(null); return; }
      const all: Scenario[] = []; let total = 1;
      while (all.length < total) {
        const page = await read<{ runs: Scenario[]; total: number }>(`/runs?limit=500&offset=${all.length}`, controller.signal);
        total = page.total; if (!page.runs.length) break; all.push(...page.runs);
      }
      if (controller.signal.aborted) return;
      setRuns(all); setRunId(previous => all.some(r => r.id === previous) ? previous : all[0]?.id ?? "");
    })().catch(e => { if (!controller.signal.aborted) setError(e.message || "The analytics service is unavailable."); })
      .finally(() => { if (!controller.signal.aborted) setLoading(false); });
    return () => controller.abort();
  }, [revision]);
  useEffect(() => {
    if (!runId || !connection?.configured) return;
    const controller = new AbortController(); setLoading(true); setBundle(null); setAt(null); setPlaying(false); setFocused(null); setError("");
    read<Bundle>(`/runs/${encodeURIComponent(runId)}`, controller.signal).then(result => { if (!controller.signal.aborted) setBundle(result); })
      .catch(e => { if (!controller.signal.aborted) setError(e.message); })
      .finally(() => { if (!controller.signal.aborted) setLoading(false); });
    return () => controller.abort();
  }, [runId, connection?.configured, revision]);
  const state = useMemo(() => bundle ? snapshot(bundle, at) : null, [bundle, at]);
  useEffect(() => {
    if (!playing || !state) return;
    const duration = state.info.duration;
    const timer = window.setInterval(() => setAt(previous => {
      const next = Math.min((previous ?? 0) + speed * 0.25, duration);
      if (next >= duration) setPlaying(false);
      return next;
    }), 250);
    return () => window.clearInterval(timer);
  }, [playing, speed, state?.info.duration]);
  const reports = state?.rows.observations ?? EMPTY;
  const incidents = state?.rows.incidents ?? EMPTY;
  const decisions = state?.rows.decisions ?? EMPTY;
  const scenario = bundle?.datasets.scenarios?.[0];
  const years = [...new Set(runs.map(r => { const t = new Date(r.started_at ?? ""); return Number.isNaN(t.getTime()) ? "" : String(t.getFullYear()); }).filter(Boolean))].sort().reverse();
  const filteredRuns = year === "all" ? runs : runs.filter(r => r.started_at && new Date(r.started_at).getFullYear() === Number(year));
  const categories = [...new Set((bundle?.datasets.events ?? []).map(e => text(e.event_type ?? e.type ?? e.table_name)).filter(x => x !== "—"))].sort();
  const dashboardEvents = (state?.visibleEvents ?? EMPTY).filter(e => eventCategory === "all" || text(e.event_type ?? e.type ?? e.table_name) === eventCategory);
  async function submitGenie(question = genieQuestion) {
    if (!runId || !question.trim()) return;
    setGenieBusy(true); setGenieError(""); setGenieReply(null);
    try { setGenieReply(await askGenie(runId, question.trim())); }
    catch (e) { setGenieError(e instanceof Error ? e.message : "Genie could not answer this question."); }
    finally { setGenieBusy(false); }
  }
  const canReplay = Boolean(state?.info.duration);
  const channels = useMemo(() => {
    const counts = new Map<string, number>();
    reports.forEach(r => { const channel = label(r.channel); counts.set(channel, (counts.get(channel) ?? 0) + 1); });
    return [...counts].sort((a, b) => b[1] - a[1]);
  }, [reports]);
  return <main className="analytics-page">
    <div className="analytics-heading"><div><span className="analytics-eyebrow">KYU-BOT / DATA PLATFORM</span><h1>Analytics & Replay</h1><p>Explore recorded scenarios. Trace the response. Compare beliefs with what happened.</p></div>
      <div className="analytics-actions">{connection?.workspace_url ? <a href={connection.workspace_url} target="_blank" rel="noreferrer">Open Databricks <ExternalLink size={14} /></a> : null}
        <button onClick={() => setRevision(r => r + 1)} disabled={loading}><RefreshCw size={14} className={loading ? "analytics-spin" : ""} /> Refresh</button></div></div>
    <div className="analytics-toolbar"><label>Year<select value={year} disabled={!runs.length || loading} onChange={e => { const next = e.target.value; setYear(next); const available = next === "all" ? runs : runs.filter(r => r.started_at && new Date(r.started_at).getFullYear() === Number(next)); if (available.length) setRunId(available[0].id); }}><option value="all">All years</option>{years.map(y => <option key={y}>{y}</option>)}</select></label><label>Scenario<select value={runId} disabled={!filteredRuns.length || loading} onChange={e => setRunId(e.target.value)}><option value="">{loading ? "Loading scenarios…" : filteredRuns.length ? "Select a scenario" : "No scenarios loaded"}</option>{filteredRuns.map(run => <option key={run.id} value={run.id}>{date(run.started_at)} · {run.id.slice(0, 8)} · {run.event_count} events</option>)}</select></label>
      <div className={`analytics-connection ${bundle ? "connected" : ""}`}><Database size={15} /><span>Databricks <b>{bundle ? "Connected" : loading ? "Checking connection" : connection?.configured ? "Waiting for data" : "Connection needed"}</b></span></div>
      {bundle ? <small>Updated {new Date(bundle.loaded_at * 1000).toLocaleTimeString()} · {runs.length} scenarios</small> : null}</div>
    <nav className="analytics-tabs" aria-label="Analytics views">{TABS.map(item => <button key={item} className={tab === item ? "active" : ""} aria-pressed={tab === item} onClick={() => setTab(item)}>{item}</button>)}</nav>
    {error ? <div className="analytics-notice error" role="alert"><strong>Data couldn’t be loaded</strong><p>{error}</p><button onClick={() => setRevision(r => r + 1)}>Try again</button></div> : null}
    {!bundle && !loading ? <section className="analytics-panel"><div className="analytics-section-heading"><h2>Natural Disaster Response Dashboard</h2><span>Workspace snapshot · live Databricks connection pending</span></div><div className="analytics-kpis">{[["Disaster events", "711"], ["Buildings damaged", "5,335"], ["People affected", "4.5M"], ["Average response score", "45%"]].map(([name, count]) => <div key={name}><span>{name}</span><strong>{count}</strong><small>From the workspace summary</small></div>)}</div><SnapshotWidgets /><div className="analytics-table-wrap"><table><thead><tr><th>Databricks dataset</th><th>Reported rows</th><th>What it contains</th></tr></thead><tbody>{SHARED_SNAPSHOT.map(([name, count, description]) => <tr key={name}><td>{name}</td><td>{count}</td><td>{description}</td></tr>)}</tbody></table></div><p className="analytics-caption">Source: Databricks workspace summary you shared. Charts with unavailable breakdowns fill from live records after the warehouse connection is configured.</p>{tab === "Replay" ? <p>Replay controls become available when recorded events are loaded.</p> : tab === "Performance" ? <p>One scenario has been scored. Live scorecards will show its measured outcomes; predictions require additional scored runs.</p> : <GenieAssistant enabled={Boolean(connection?.genie_configured && runId)} busy={genieBusy} question={genieQuestion} setQuestion={setGenieQuestion} submit={submitGenie} reply={genieReply} error={genieError} />}</section> : null}
    {!connection?.configured && !loading && !error ? <div className="analytics-connect"><Database size={32} /><h2>Connect Arwa’s Databricks workspace</h2><p>This platform is ready for your scenarios, exported reports, simulation truth, and evaluation scores.</p><p className="analytics-caption">Workspace 7474645633187109 · KnightHacks catalog</p><p>Once the workspace connection is configured on the server, refresh to load your records.</p></div> : null}
    {loading ? <div className="analytics-loading" role="status"><RefreshCw size={20} className="analytics-spin" /> Loading Databricks data…</div> : null}
    {connection?.configured && !loading && !error && !runs.length ? <div className="analytics-notice"><h2>No scenarios have been uploaded yet</h2><p>Upload a simulation to the KnightHacks truth tables, then refresh this page.</p></div> : null}
    {bundle && state ? <>
      <div className="analytics-run-meta"><span>Run <code>{runId}</code></span><span>Map {text(scenario?.map_version_id)}</span><span>Magnitude {text(object(scenario?.params).mw)}</span><span>Seed {text(scenario?.seed)}</span></div>
      <section className="analytics-replay-controls" aria-label="Recorded replay controls"><button disabled={!canReplay} aria-label={playing ? "Pause replay" : "Play replay"} onClick={() => { if (!playing && (at === null || at >= state.info.duration)) setAt(0); setPlaying(p => !p); }}>{playing ? <Pause size={16} /> : <Play size={16} />}</button><div><strong>{at === null ? "Latest exported state" : `Recorded +${clock(at)}`}</strong><span>{state.simTime === null ? "Simulation time unavailable" : `Last recorded simulation time T+${clock(state.simTime)}`}</span></div><input aria-label="Recorded elapsed seconds" type="range" min={0} max={Math.max(state.info.duration, 1)} step={0.1} value={at ?? state.info.duration} disabled={!canReplay} onChange={e => { setPlaying(false); setAt(Number(e.target.value)); }} /><label>Speed<select value={speed} onChange={e => setSpeed(Number(e.target.value))}>{[1, 5, 10, 30].map(s => <option key={s} value={s}>{s}×</option>)}</select></label><button onClick={() => { setPlaying(false); setAt(null); }}>Latest</button></section>
      <p className="analytics-caption">Replay uses recorded event timestamps. Truth during replay is limited to the last recorded simulation time. History reflects the latest Databricks export.{state.unknownTimes ? ` ${state.unknownTimes} events have no recorded timestamp and only appear in the latest view.` : ""}</p>
      {tab === "Overview" || tab === "Replay" ? <>
        {tab === "Overview" ? <>
          <div className="analytics-widget-toolbar"><div><strong>Dashboard filters</strong><span>Filter the selected run’s Databricks records</span></div><label>Event category<select value={eventCategory} onChange={e => setEventCategory(e.target.value)}><option value="all">All categories</option>{categories.map(c => <option key={c} value={c}>{label(c)}</option>)}</select></label></div>
          <DisasterWidgets events={dashboardEvents} damage={state.damage} people={state.people} runs={runs} scorecards={bundle.datasets.scorecards ?? EMPTY} />
          <GenieAssistant enabled={Boolean(connection?.genie_configured)} busy={genieBusy} question={genieQuestion} setQuestion={setGenieQuestion} submit={submitGenie} reply={genieReply} error={genieError} />
        </> : null}
        <div className="analytics-kpis">{[["Reports released", reports.length], ["Agent incidents", incidents.length], ["Response decisions", decisions.length], ["Recorded events", state.visibleEvents.length]].map(([name, value]) => <div key={name}><span>{name}</span><strong>{Number(value).toLocaleString()}</strong><small>At selected replay point</small></div>)}</div>
        <div className="analytics-grid"><section className="analytics-panel"><div className="analytics-section-heading"><h2>Response replay map</h2><span>Reports & incidents</span></div><ReplayMap reports={reports} incidents={incidents} focused={focused} /></section>
          <section className="analytics-panel"><div className="analytics-section-heading"><h2>Reports by source</h2><span>{reports.length} reports</span></div>{channels.length ? <div className="analytics-bars">{channels.map(([name, value]) => <div key={name}><span>{name}</span><div><i style={{ width: `${value / Math.max(...channels.map(c => c[1])) * 100}%` }} /></div><b>{value}</b></div>)}</div> : <p className="analytics-empty">No exported reports at this replay point.</p>}<p className="analytics-caption">Databricks history · count by report channel</p><h3>Simulation truth at this point</h3><div className="analytics-truth-summary"><span><b>{state.damage.filter(d => d.damage_level !== "none").length}</b> damaged buildings</span><span><b>{state.roads.filter(r => r.status !== "OPEN").length}</b> restricted roads</span><span><b>{state.people.reduce((sum, p) => sum + (number(p.count) ?? 0), 0)}</b> people in recorded groups</span></div></section></div>
        <div className="analytics-grid"><section className="analytics-panel"><h2>Latest recorded reports</h2>{reports.length ? <div className="analytics-report-feed">{[...reports].reverse().slice(0, 8).map(report => <button key={text(report.id)} onClick={() => setFocused(report)}><span>{label(report.channel)} · {number(report.sim_time_s) === null ? "Time unknown" : `T+${clock(Number(report.sim_time_s))}`}</span><p>{text(report.text_en ?? report.original_text)}</p><small>{text(report.claimed_location)}</small></button>)}</div> : <p className="analytics-empty">Reports appear here after operational history has been exported.</p>}</section><section className="analytics-panel"><h2>Reported vs. actual</h2><p className="analytics-caption">Simulation labels for released reports. These are evaluation labels, not source confidence scores.</p><Comparison reports={reports} links={bundle.datasets.truth_links ?? EMPTY} /><div className="analytics-section-heading"><h3>Decision trail</h3><button onClick={() => setTab("Data explorer")}>Explore all records →</button></div>{decisions.slice(-5).reverse().map(d => <div className="analytics-decision" key={text(d.id)}><strong>{label(d.status)} · version {text(d.version)}</strong><small>{text(d.incident_id)} · ETA {number(d.eta_s) === null ? "unknown" : `${Math.round(Number(d.eta_s))} s`}</small></div>)}{!decisions.length ? <p className="analytics-empty">No decisions at this replay point.</p> : null}</section></div>
      </> : null}
      {tab === "Data explorer" ? <Explorer bundle={bundle} state={state} at={at} /> : null}
      {tab === "Performance" ? <Performance rows={bundle.datasets.scorecards ?? EMPTY} /> : null}
    </> : null}
    <p className="analytics-source-note"><Database size={12} /> Analytics source: Databricks · knighthacks.truth / history / eval · Operator view</p>
  </main>;
}

function DisasterWidgets({ events, damage, people, runs, scorecards }: { events: Row[]; damage: Row[]; people: Row[]; runs: Scenario[]; scorecards: Row[] }) {
  const eventTypes = tally(events, e => text(e.event_type ?? e.type ?? e.table_name));
  const damageLevels = tally(damage, r => text(r.damage_level ?? r.severity ?? r.damage_state));
  const peopleStates = tally(people, r => text(r.status ?? r.condition ?? r.state));
  const magnitudes = tally(runs, r => { const p = object(r.params); return `Mw ${text(p.mw ?? p.magnitude)}`; });
  const rates = scorecards.filter(r => METRICS[text(r.metric)]?.[1]).map(r => number(r.value)).filter((v): v is number => v !== null);
  const avgScore = rates.length ? rates.reduce((a, b) => a + b, 0) / rates.length : null;
  const totalPeople = people.reduce((n, r) => n + (number(r.count) ?? 1), 0);
  const timeline = Array.from({ length: 12 }, () => 0);
  const times = events.map(e => eventTime(e, timestampOrigin(events))).filter((v): v is number => v !== null);
  const maxTime = times.length ? Math.max(...times) : 0;
  events.forEach(e => { const t = eventTime(e, timestampOrigin(events)); if (t !== null) timeline[maxTime === 0 ? 0 : Math.min(11, Math.floor(t / maxTime * 12))]++; });
  const sum = eventTypes.reduce((n, x) => n + x.value, 0);
  let cursor = 0;
  const colors = ["#bd4a43", "#396e86", "#d5a46e", "#6a927b", "#8e78a9", "#8799a6"];
  const pie = sum ? `conic-gradient(${eventTypes.map((item, i) => { const start = cursor; cursor += item.value / sum * 100; return `${colors[i % colors.length]} ${start}% ${cursor}%`; }).join(",")})` : "#f0f1f2";
  return <>
    <div className="analytics-kpis analytics-dashboard-kpis">{[["Disaster events", events.length.toLocaleString(), "Selected scenario"], ["Buildings damaged", damage.filter(r => !["none", "undamaged", "no_damage"].includes(text(r.damage_level).toLowerCase())).length.toLocaleString(), `${damage.length.toLocaleString()} truth records`], ["People represented", totalPeople.toLocaleString(), `${people.length.toLocaleString()} truth records`], ["Average response score", avgScore === null ? "—" : `${(avgScore * 100).toFixed(1)}%`, avgScore === null ? "No rate metrics scored" : `${rates.length} rate measurements`]].map(([name, value, note]) => <div key={name}><span>{name}</span><strong>{value}</strong><small>{note}</small></div>)}</div>
    <div className="analytics-grid analytics-widget-grid">
      <section className="analytics-panel"><div className="analytics-section-heading"><h2>Disaster events by category</h2><span>{sum.toLocaleString()} records</span></div>{sum ? <div className="analytics-donut-row"><div className="analytics-donut" style={{ background: pie }} role="img" aria-label={`Distribution of ${sum} disaster events`}><span>{sum}<small>events</small></span></div><Legend items={eventTypes} colors={colors} /></div> : <EmptyChart />}</section>
      <section className="analytics-panel"><div className="analytics-section-heading"><h2>Event timeline</h2><span>Recorded time · 12 intervals</span></div>{sum ? <div className="analytics-timeline" role="img" aria-label="Recorded events across the scenario timeline">{timeline.map((v, i) => <div key={i}><i style={{ height: `${Math.max(v ? 8 : 2, (v / Math.max(1, ...timeline)) * 100)}%` }} /><small>{i + 1}</small></div>)}</div> : <EmptyChart />}</section>
      <section className="analytics-panel"><div className="analytics-section-heading"><h2>Building damage severity</h2><span>{damage.length.toLocaleString()} truth records</span></div>{damageLevels.length ? <MetricBars items={damageLevels} /> : <EmptyChart />}</section>
      <section className="analytics-panel"><div className="analytics-section-heading"><h2>People impact</h2><span>{people.length.toLocaleString()} truth records</span></div>{peopleStates.length ? <MetricBars items={peopleStates} /> : <EmptyChart />}</section>
      <section className="analytics-panel"><div className="analytics-section-heading"><h2>Scenarios by magnitude</h2><span>{runs.length} scenarios</span></div>{magnitudes.length ? <MetricBars items={magnitudes} /> : <EmptyChart />}</section>
      <section className="analytics-panel"><div className="analytics-section-heading"><h2>Agent response gaps</h2><span>Rate metrics only</span></div>{scorecards.length ? <MetricBars items={scorecards.filter(r => METRICS[text(r.metric)]?.[1]).map(r => ({ name: METRICS[text(r.metric)][0], value: Math.max(0, Math.round((1 - (number(r.value) ?? 0)) * 100)) })).sort((a,b) => b.value-a.value)} suffix="% gap" /> : <EmptyChart text="Score the scenario in Databricks to see response gaps." />}</section>
    </div>
  </>;
}
function timestampOrigin(events: Row[]) { const dates = events.map(e => timestamp(e.created_at)).filter((v): v is number => v !== null); return dates.length ? Math.min(...dates) : 0; }
function tally(rows: Row[], key: (row: Row) => string) { const map = new Map<string, number>(); rows.forEach(row => { const value = key(row); if (value !== "—" && value !== "null" && value !== "undefined") map.set(value, (map.get(value) ?? 0) + (number(row.count) ?? 1)); }); return [...map].map(([name, value]) => ({ name, value })).sort((a,b) => b.value-a.value); }
function Legend({ items, colors }: { items: { name: string; value: number }[]; colors: string[] }) { return <div className="analytics-legend">{items.slice(0, 7).map((item, i) => <div key={item.name}><i style={{ background: colors[i % colors.length] }} /><span>{label(item.name)}</span><b>{item.value.toLocaleString()}</b></div>)}</div>; }
function MetricBars({ items, suffix = "" }: { items: { name: string; value: number }[]; suffix?: string }) { const max = Math.max(1, ...items.map(i => i.value)); return <div className="analytics-metric-bars">{items.slice(0, 8).map((item, i) => <div key={`${item.name}-${i}`}><span>{label(item.name)}</span><div><i style={{ width: `${Math.max(1, item.value / max * 100)}%` }} /></div><b>{item.value.toLocaleString()}{suffix}</b></div>)}</div>; }
function EmptyChart({ text: message = "No records in this selection." }: { text?: string }) { return <div className="analytics-chart-empty"><BarChart3 size={19} /><span>{message}</span></div>; }
function SnapshotWidgets() { return <div className="analytics-grid analytics-widget-grid analytics-snapshot-widgets"><section className="analytics-panel"><h2>Events by type</h2><div className="analytics-snapshot-bar"><span>Observations</span><i style={{ width: "67.4%" }} /><b>479</b></div><div className="analytics-snapshot-bar"><span>Other exported events</span><i style={{ width: "32.6%" }} /><b>232</b></div><p className="analytics-caption">The shared summary reports 479 observations out of 711 events.</p></section><section className="analytics-panel"><h2>Building damage severity</h2><strong className="analytics-snapshot-stat">5,335</strong><p className="analytics-caption">Reported damaged buildings. Severity categories will load from truth tables.</p><EmptyChart text="Severity breakdown requires the live connection." /></section><section className="analytics-panel"><h2>People impact</h2><strong className="analytics-snapshot-stat">4.5M</strong><p className="analytics-caption">Reported people affected. Condition breakdown will load from truth tables.</p><EmptyChart text="Status breakdown requires the live connection." /></section><section className="analytics-panel"><h2>Scenarios by magnitude</h2><div className="analytics-snapshot-bar"><span>Mw 7.0</span><i style={{ width: "78.9%" }} /><b>15</b></div><div className="analytics-snapshot-bar"><span>Other magnitudes</span><i style={{ width: "21.1%" }} /><b>4</b></div><p className="analytics-caption">15 of 19 scenarios are Mw 7.0, per the shared workspace summary.</p></section><section className="analytics-panel"><h2>Response gap</h2><div className="analytics-snapshot-bar"><span>Road block recall gap</span><i style={{ width: "100%" }} /><b>100%</b></div><div className="analytics-snapshot-bar"><span>Road block precision gap</span><i style={{ width: "100%" }} /><b>100%</b></div><p className="analytics-caption">Two largest gaps from the supplied scorecard summary.</p></section><section className="analytics-panel"><h2>Events over time</h2><EmptyChart text="Recorded timestamps will load from the exported event history." /></section></div>; }
function GenieAssistant({ enabled, busy, question, setQuestion, submit, reply, error }: { enabled: boolean; busy: boolean; question: string; setQuestion: (value: string) => void; submit: (question?: string) => void; reply: GenieReply | null; error: string }) {
  const prompts = ["Which event categories were most common?", "What were the biggest response gaps?", "Summarize the building damage and people impact."];
  return <section className="analytics-panel analytics-genie"><div className="analytics-section-heading"><div><h2><Sparkles size={17} /> Ask Genie</h2><p className="analytics-caption">Ask about this selected scenario. Genie can read your Databricks data.</p></div><span>{enabled ? "Connected to Databricks Genie" : "Genie connection needed"}</span></div><div className="analytics-genie-prompts">{prompts.map(p => <button key={p} disabled={!enabled || busy} onClick={() => { setQuestion(p); submit(p); }}>{p}</button>)}</div><form onSubmit={e => { e.preventDefault(); submit(); }}><input value={question} onChange={e => setQuestion(e.target.value)} placeholder={enabled ? "Ask about this disaster response…" : "Configure a Genie space to ask questions"} disabled={!enabled || busy} /><button disabled={!enabled || busy || !question.trim()}><Send size={14} /> {busy ? "Asking…" : "Ask"}</button></form>{error ? <p className="analytics-genie-error" role="alert">{error}</p> : null}{reply ? <div className="analytics-genie-answer"><strong>Genie’s answer</strong><p>{reply.text || "Genie returned query results."}</p>{reply.queries.map((q,i) => <details key={i}><summary>{q.description || `Databricks query ${i+1}`} · {q.rows.length} rows</summary><pre>{q.sql}</pre>{q.rows.length ? <pre>{JSON.stringify(q.rows.slice(0,20), null, 2)}</pre> : null}</details>)}</div> : null}</section>;
}

function Comparison({ reports, links }: { reports: Row[]; links: Row[] }) {
  const ids = new Set(reports.map(r => text(r.id)));
  const grouped = new Map<string, Set<string>>();
  for (const link of links) { const id = text(link.observation_id); if (ids.has(id)) { if (!grouped.has(id)) grouped.set(id, new Set()); grouped.get(id)!.add(text(link.match_type)); } }
  const counts = new Map<string, number>();
  for (const kinds of grouped.values()) { const kind = kinds.size > 1 ? "mixed evidence" : [...kinds][0]; counts.set(kind, (counts.get(kind) ?? 0) + 1); }
  const missing = ids.size - grouped.size;
  return <div className="analytics-comparison">{[...counts].map(([kind, count]) => <div key={kind}><span>{label(kind)}</span><strong>{count}</strong></div>)}{missing ? <div><span>Truth link unavailable</span><strong>{missing}</strong></div> : null}{!ids.size ? <p className="analytics-empty">Select a point with released reports to compare them.</p> : null}</div>;
}
function Explorer({ bundle, state, at }: { bundle: Bundle; state: ReturnType<typeof snapshot>; at: number | null }) {
  const [dataset, setDataset] = useState("observations"); const [search, setSearch] = useState(""); const [page, setPage] = useState(0); const [selected, setSelected] = useState<Row | null>(null);
  const options: Record<string, Row[]> = { observations: state.rows.observations ?? EMPTY, incidents: state.rows.incidents ?? EMPTY, decisions: state.rows.decisions ?? EMPTY, resources: state.rows.resources ?? EMPTY, agent_runs: state.rows.agent_runs ?? EMPTY, road_restrictions: state.rows.road_restrictions ?? EMPTY, operational_events: state.visibleEvents, building_damage: state.damage, road_truth: state.roads, people_truth: state.people, truth_links: (bundle.datasets.truth_links ?? EMPTY).filter(l => (state.rows.observations ?? EMPTY).some(o => text(o.id) === text(l.observation_id))), scorecards: bundle.datasets.scorecards ?? EMPTY };
  const rows = options[dataset] ?? EMPTY;
  const filtered = useMemo(() => rows.filter(r => JSON.stringify(r).toLowerCase().includes(search.toLowerCase())), [rows, search]);
  useEffect(() => { setPage(0); setSelected(null); }, [dataset, search, bundle.run_id, at]);
  const safePage = Math.min(page, Math.max(0, Math.ceil(filtered.length / 25) - 1));
  const columns = [...new Set(filtered.slice(0, 30).flatMap(row => Object.keys(row)))].filter(k => !k.startsWith("_")).slice(0, 7);
  return <section className="analytics-panel"><div className="analytics-section-heading"><div><h2>Data explorer</h2><p className="analytics-caption">Select a row to inspect every field. CSV includes all fields and all matching rows.</p></div><button disabled={!filtered.length} onClick={() => download(filtered, `${bundle.run_id}-${dataset}.csv`)}><Download size={14} /> Export CSV</button></div><div className="analytics-explorer-controls"><label>Dataset<select value={dataset} onChange={e => setDataset(e.target.value)}>{Object.entries(options).map(([key, data]) => <option key={key} value={key}>{label(key)} ({data.length})</option>)}</select></label><label className="analytics-search"><Search size={15} /><input value={search} placeholder="Search all fields…" onChange={e => setSearch(e.target.value)} /></label><span>{filtered.length.toLocaleString()} records</span></div>{filtered.length ? <div className="analytics-table-wrap"><table><thead><tr>{columns.map(key => <th key={key}>{label(key)}</th>)}</tr></thead><tbody>{filtered.slice(safePage * 25, (safePage + 1) * 25).map((row, i) => <tr key={text(row.id ?? row.event_id ?? row.metric ?? i)}>{columns.map((key, c) => <td key={key}>{c === 0 ? <button className="analytics-row-button" onClick={() => setSelected(row)}>{text(row[key])}</button> : <span title={text(row[key])}>{text(row[key])}</span>}</td>)}</tr>)}</tbody></table></div> : <p className="analytics-empty">No matching records at this replay point.</p>}<div className="analytics-pagination"><button disabled={!safePage} onClick={() => setPage(p => p - 1)}>Previous</button><span>{filtered.length ? `${safePage * 25 + 1}–${Math.min((safePage + 1) * 25, filtered.length)} of ${filtered.length}` : "0 records"}</span><button disabled={(safePage + 1) * 25 >= filtered.length} onClick={() => setPage(p => p + 1)}>Next</button></div>{selected ? <div className="analytics-record-detail"><div className="analytics-section-heading"><h3>Complete record</h3><button onClick={() => setSelected(null)}>Close</button></div><pre>{JSON.stringify(selected, null, 2)}</pre></div> : null}</section>;
}
function Performance({ rows }: { rows: Row[] }) {
  const [config, setConfig] = useState("all");
  const configs = [...new Set(rows.map(r => text(r.agent_config)))];
  const filtered = rows.filter(row => config === "all" || text(row.agent_config) === config);
  return <section className="analytics-panel"><div className="analytics-section-heading"><div><h2>Agent performance</h2><p className="analytics-caption">Run-level scorecards from Databricks. Scores do not change with the replay slider.</p></div><label>Agent configuration<select value={config} onChange={e => setConfig(e.target.value)}><option value="all">All configurations</option>{configs.map(c => <option key={c}>{c}</option>)}</select></label></div>{filtered.length ? <div className="analytics-score-grid">{filtered.map((row, i) => { const metric = text(row.metric); const value = number(row.value); const [name, rate] = METRICS[metric] ?? [label(metric), false]; return <article key={`${metric}-${text(row.agent_config)}-${i}`}><span>{name}</span><strong>{value === null ? "Not measurable" : rate ? `${(value * 100).toFixed(1)}%` : value.toLocaleString()}</strong>{rate && value !== null ? <progress max={1} value={value} aria-label={name} /> : null}<small>{text(row.agent_config)} · {date(row.scored_at)}</small><details><summary>How this score was calculated</summary><pre>{JSON.stringify(row.details ?? {}, null, 2)}</pre></details></article>; })}</div> : <div className="analytics-empty"><BarChart3 size={24} /><h3>This run has not been scored yet</h3><p>Scorecards will appear after evaluation has been run in Databricks.</p></div>}</section>;
}
