import { useRef, useState } from "react";
import { Check, Crosshair, Loader2, MapPin, Plane, Send, X } from "lucide-react";
import type { Decision, OperationalState, Priority, ReconResult, Report } from "./map/dataSource";
import { flyRecon, reconImage, sendReconReport } from "./map/dataSource";

type Incident = OperationalState["incidents"][number];
export interface QueueEntry { incident: Incident; priority: Priority; trapped: number; critical: number; injured: number; waiting: boolean }

/** Why an incident is worth a drone pass: the three things reports alone cannot settle. */
function doubts(incident: Incident, waiting: boolean): string[] {
  const supports = incident.evidence?.supports.length ?? 0;
  const out: string[] = [];
  if (waiting) out.push("Unit waiting on your approval");
  if (!incident.building_id) out.push("Exact building unknown");
  if (incident.status === "disputed") out.push("Reports disagree");
  else if (supports <= 1) out.push("Only one report");
  return out;
}

const DAMAGE_LABEL: Record<string, string> = {
  none: "No damage seen", light: "Light damage", moderate: "Moderate damage", severe: "Severe damage", collapsed: "Collapse",
};

/** Does what the drone saw back up what the agents believe? */
function verdict(incident: Incident, r: ReconResult, trapped: number): { tone: "confirm" | "doubt" | "unclear"; text: string } {
  const seen = r.reading.damage_seen;
  if (!seen || r.reading.confidence == null || r.reading.confidence < 0.4) return { tone: "unclear", text: "Drone view inconclusive" };
  const heavy = seen === "severe" || seen === "collapsed";
  if (heavy) return { tone: "confirm", text: trapped ? "Supports reports of people trapped" : "Heavy damage: check for people" };
  if (seen === "none" || seen === "light")
    return { tone: "doubt", text: incident.status === "disputed" || trapped ? "Does not match the reports" : "No sign of serious damage" };
  return { tone: "unclear", text: "Damage seen, collapse not visible" };
}

export function ReconPage(props: {
  queue: QueueEntry[];
  reports: Report[];
  decisions: Decision[];
  results: Record<string, ReconResult>;
  onResult: (incidentId: string, r: ReconResult) => void;
  selectedId: string | null;
  onSelect: (id: string) => void;
  buildingName: (id: string) => string | null | undefined;
  onShowOnMap: (lon: number, lat: number) => void;
  runId: string | null;
  acting: Record<string, string>;
  onDecide: (d: Decision, incident: Incident, approve: boolean) => void;
  unitName: (id: string) => string;
}) {
  const { queue, reports, results, selectedId } = props;
  // Most worth a look first: a unit waiting on approval, then the most doubts, then priority order.
  const ranked = [...queue].map((q) => ({ ...q, doubts: doubts(q.incident, q.waiting) }))
    .sort((a, b) => Number(b.waiting) - Number(a.waiting) || b.doubts.length - a.doubts.length);
  const current = ranked.find((q) => q.incident.id === selectedId) ?? ranked[0] ?? null;
  const result = current ? results[current.incident.id] : undefined;
  const [flying, setFlying] = useState<Record<string, string>>({});   // incident id -> "flying" | error
  const [sending, setSending] = useState<Record<string, string>>({});  // recon id -> "sending" | "sent" | error

  const fly = async (q: QueueEntry) => {
    const at = q.incident.location?.coordinates;
    if (!at) return;
    const ids = new Set(q.incident.evidence?.supports ?? []);
    const context = reports.filter((r) => ids.has(r.id)).map((r) => r.text_en || r.original_text || "").filter(Boolean)
      .join(" | ").slice(0, 1400);
    setFlying((f) => ({ ...f, [q.incident.id]: "flying" }));
    try {
      const r = await flyRecon({ incident_id: q.incident.id, building_id: q.incident.building_id, lon: at[0], lat: at[1], context });
      props.onResult(q.incident.id, r);
      setFlying((f) => { const { [q.incident.id]: _, ...rest } = f; return rest; });
    } catch (e) {
      setFlying((f) => ({ ...f, [q.incident.id]: (e as Error).message }));
    }
  };
  const send = async (r: ReconResult) => {
    setSending((s) => ({ ...s, [r.id]: "sending" }));
    try {
      await sendReconReport(r.id);
      setSending((s) => ({ ...s, [r.id]: "sent" }));
    } catch (e) {
      setSending((s) => ({ ...s, [r.id]: (e as Error).message }));
    }
  };

  return (
    <main className="recon-page">
      <aside className="recon-list">
        <div className="recon-list-head">
          <span>NEEDS EYES ON IT</span>
          <small>Incidents the reports cannot settle: where exactly, is it real, can units get in.</small>
        </div>
        {ranked.map((q) => {
          const done = results[q.incident.id];
          const where = q.incident.building_id ? props.buildingName(q.incident.building_id) : null;
          return (
            <button key={q.incident.id} onClick={() => props.onSelect(q.incident.id)}
              className={`recon-item${current?.incident.id === q.incident.id ? " active" : ""}`}>
              <span className={`priority-badge priority-${q.priority}`}>{q.priority.toUpperCase()}</span>
              <strong>{[q.trapped && `${q.trapped} trapped`, q.critical && `${q.critical} critical`, q.injured && `${q.injured} injured`]
                .filter(Boolean).join(" · ") || "Damage, no casualties known"}</strong>
              <small>{where ?? "Location from reports"}</small>
              <span className="recon-doubts">
                {done ? <em className="recon-flown"><Check size={10} /> Flown · {DAMAGE_LABEL[done.reading.damage_seen ?? ""] ?? "unclear"}</em>
                  : q.doubts.map((d) => <em key={d}>{d}</em>)}
              </span>
            </button>
          );
        })}
        {ranked.length === 0 && <div className="panel-empty">{props.runId ? "No incidents yet." : "No run is playing."}</div>}
      </aside>

      <section className="recon-view">
        {!current ? (
          <div className="recon-empty"><Plane size={22} /><p>Pick an incident to fly the drone over it.</p></div>
        ) : result ? (
          <Compare before={reconImage(result.before)} after={reconImage(result.after)} />
        ) : (
          <div className="recon-preview">
            {current.incident.location && <AerialPreview lon={current.incident.location.coordinates[0]} lat={current.incident.location.coordinates[1]} />}
            <span className="recon-tag top left">REAL AERIAL PHOTO · BEFORE THE EARTHQUAKE</span>
            <div className="recon-overlay">
              <strong>Fly the drone over this incident</strong>
              <p>See what changed since this photo, and get the vision model's reading: which building, how bad, which streets are passable.</p>
              {flying[current.incident.id] === "flying"
                ? <button className="recon-fly" disabled><Loader2 size={14} className="spin" /> Drone flying… usually 15–60 s</button>
                : <button className="recon-fly" disabled={!current.incident.location} onClick={() => fly(current)}>
                    <Plane size={14} /> Fly drone
                  </button>}
              {flying[current.incident.id] && flying[current.incident.id] !== "flying" && (
                <em className="proposal-error">
                  {/not being played|No run/.test(flying[current.incident.id])
                    ? "The drone flies over the live earthquake. Click Generate on the Operations tab first."
                    : flying[current.incident.id]}
                </em>
              )}
            </div>
          </div>
        )}
        <div className="recon-credit">
          Before: GSI seamless aerial photo (Geospatial Information Authority of Japan). After: SIMULATED drone image drawn
          from the scenario, not a real photo.
        </div>
      </section>

      <aside className="recon-reading">
        {current && result ? (
          <Reading q={current} r={result} sending={sending[result.id]} onSend={() => send(result)}
            proposals={props.decisions.filter((d) => d.status === "draft" && d.incident_id === current.incident.id)}
            acting={props.acting} onDecide={props.onDecide} unitName={props.unitName}
            onFlyAgain={() => fly(current)} flying={flying[current.incident.id] === "flying"}
            onShowOnMap={() => props.onShowOnMap(result.longitude, result.latitude)} />
        ) : (
          <div className="recon-why">
            <span className="recon-section">WHY A DRONE</span>
            <p><b>Where exactly?</b> A report "near the station" fits dozens of buildings. The drone marks the damaged one.</p>
            <p><b>Is it real?</b> Many incidents rest on one report, some of them rumours. Units are scarce; the drone checks before one is sent.</p>
            <p><b>Can we get in?</b> Routes use the roads believed open. The drone sees debris on the approach streets before a unit drives into it.</p>
            <p className="recon-note">Its reading can go to the agents as a drone report, with the other evidence. You still approve every dispatch.</p>
          </div>
        )}
      </aside>
    </main>
  );
}

function recommendation(tone: "confirm" | "doubt" | "unclear", r: ReconResult): string {
  if (tone === "confirm") return `Approve: the drone confirms heavy damage.${r.reading.approach ? ` ${r.reading.approach}` : ""}`;
  if (tone === "doubt") return "Hold or reject: the drone saw no serious damage here. The unit may be needed elsewhere.";
  return "Your call: the drone view is not clear enough to confirm or rule out the reports.";
}

function Reading(props: { q: QueueEntry; r: ReconResult; sending?: string; onSend: () => void; onFlyAgain: () => void;
                          flying: boolean; onShowOnMap: () => void; proposals: Decision[]; acting: Record<string, string>;
                          onDecide: (d: Decision, incident: Incident, approve: boolean) => void; unitName: (id: string) => string }) {
  const { q, r, sending } = props;
  const v = verdict(q.incident, r, q.trapped + q.critical);
  const match = r.candidates.find((c) => c.label === r.reading.building);
  const conf = Math.round((r.reading.confidence ?? 0) * 100);
  return (
    <div className="recon-card">
      <span className="recon-section">WHAT THE DRONE SAW</span>
      <div className={`recon-verdict ${v.tone}`}>{v.text}</div>
      <div className="recon-damage">
        <span className={`damage-pill damage-${r.reading.damage_seen ?? "unknown"}`}>
          {DAMAGE_LABEL[r.reading.damage_seen ?? ""] ?? "Unclear"}
        </span>
        <span className="recon-conf">
          <i style={{ width: `${conf}%` }} />
          <small>{conf}% sure</small>
        </span>
      </div>
      {r.reading.summary && <p className="recon-summary">{r.reading.summary}</p>}

      <details className="recon-details">
        <summary>Details: buildings, streets, approach</summary>
        <span className="recon-section">BUILDINGS (A = the reported one)</span>
        <p>
          {match ? <><b>{match.label}</b> · {match.name ?? match.usage?.replace(/_/g, " ") ?? "building"}{match.floors ? ` · ${match.floors} floors` : ""}</>
            : "None of the marked buildings is damaged"}
        </p>
        {Object.keys(r.reading.buildings ?? {}).length > 0 && (
          <div className="recon-buildings">
            {r.candidates.map((c) => {
              const call = r.reading.buildings[c.label] ?? "unclear";
              return <span key={c.label} className={`damage-${call}`} title={c.name ?? c.usage ?? ""}><b>{c.label}</b> {call}</span>;
            })}
          </div>
        )}
        <span className="recon-section">STREETS</span>
        {r.reading.debris_on_streets.length ? (
          <ul className="recon-streets">
            {r.reading.debris_on_streets.map((d, i) => (
              <li key={i}><span>{d.where}</span><b className={`pass-${(d.passable ?? "").replace(/\s/g, "-")}`}>{d.passable ?? "?"}</b></li>
            ))}
          </ul>
        ) : <p>No new rubble on the streets.</p>}
        {r.reading.approach && <><span className="recon-section">APPROACH</span><p>{r.reading.approach}</p></>}
        {r.reading.staging && <><span className="recon-section">STAGING</span><p>{r.reading.staging}</p></>}
      </details>

      <div className="recon-decision">
        <span className="recon-section">YOUR DECISION</span>
        <p className={`recon-reco ${v.tone}`}>{recommendation(v.tone, r)}</p>
        {props.proposals.length ? props.proposals.map((d) => {
          const state = props.acting[d.id];
          return (
            <div className="proposal" key={d.id}>
              <span>
                Agent proposes <b>{props.unitName(d.assignments?.[0]?.resource_id ?? "")}</b>
                {d.eta_s != null ? ` · ETA ${Math.max(1, Math.round(d.eta_s / 60))} min` : ""}
              </span>
              {state && !["done", "sending", "rejecting"].includes(state)
                ? <em className="proposal-error">{state}</em>
                : state === "done" ? <em className="recon-done">Done</em> : (
                  <span className="proposal-actions">
                    <button className="approve" disabled={Boolean(state)} onClick={() => props.onDecide(d, q.incident, true)}>
                      <Check size={12} /> {state === "sending" ? "Sending…" : "Approve"}
                    </button>
                    <button className="reject" disabled={Boolean(state)} onClick={() => props.onDecide(d, q.incident, false)}>
                      <X size={12} /> Reject
                    </button>
                  </span>
                )}
            </div>
          );
        }) : <p className="recon-note-small">No unit proposed for this incident yet. Send the drone report and the agents will weigh it.</p>}
        <button className="recon-send" disabled={sending === "sending" || sending === "sent"} onClick={props.onSend}>
          {sending === "sent" ? <><Check size={13} /> Drone report sent to the agents</>
            : sending === "sending" ? <><Loader2 size={13} className="spin" /> Sending…</>
              : <><Send size={13} /> Send drone report to the agents</>}
        </button>
        {sending && !["sent", "sending"].includes(sending) && <em className="proposal-error">{sending}</em>}
        <div className="recon-secondary">
          <button onClick={props.onShowOnMap}><MapPin size={12} /> Show on map</button>
          <button onClick={props.onFlyAgain} disabled={props.flying}>
            {props.flying ? <Loader2 size={12} className="spin" /> : <Crosshair size={12} />} Fly again
          </button>
        </div>
      </div>
      <small className="recon-meta">Read by Qwen3-VL in {r.seconds}s · T+{Math.floor(r.sim_time_s / 60)} min</small>
    </div>
  );
}

/** Before / after with a slider: drag to wipe between the real photo and the simulated drone view. */
function Compare({ before, after }: { before: string; after: string }) {
  const [split, setSplit] = useState(50);
  const box = useRef<HTMLDivElement>(null);
  const move = (clientX: number) => {
    const b = box.current?.getBoundingClientRect();
    if (b) setSplit(Math.max(0, Math.min(100, ((clientX - b.left) / b.width) * 100)));
  };
  return (
    <div className="recon-compare" ref={box}
      onPointerDown={(e) => { (e.target as HTMLElement).setPointerCapture?.(e.pointerId); move(e.clientX); }}
      onPointerMove={(e) => e.buttons && move(e.clientX)}>
      <img src={after} alt="Simulated drone image after the earthquake" draggable={false} />
      <img src={before} alt="Aerial photo before the earthquake" draggable={false}
        style={{ clipPath: `inset(0 ${100 - split}% 0 0)` }} />
      <div className="recon-divider" style={{ left: `${split}%` }}><span>⟷</span></div>
      <span className="recon-tag top left">◀ BEFORE · real aerial photo</span>
      <span className="recon-tag top right">AFTER · drone, simulated ▶</span>
      <span className="recon-hint">Drag to compare</span>
    </div>
  );
}

/** The real GSI aerial photo around a point, straight from the public tiles (zoom 18, ~0.5 m per pixel). */
function AerialPreview({ lon, lat }: { lon: number; lat: number }) {
  const z = 18, n = 2 ** z;
  const fx = ((lon + 180) / 360) * n;
  const fy = ((1 - Math.asinh(Math.tan((lat * Math.PI) / 180)) / Math.PI) / 2) * n;
  const tx = Math.floor(fx), ty = Math.floor(fy);
  // a 5x5 block of tiles, shifted so the point sits at the centre of the panel
  const shiftX = -(512 + (fx - tx) * 256), shiftY = -(512 + (fy - ty) * 256);
  const tiles = [];
  for (let dy = -2; dy <= 2; dy++)
    for (let dx = -2; dx <= 2; dx++)
      tiles.push(<img key={`${dx},${dy}`} alt="" draggable={false} style={{ left: (dx + 2) * 256, top: (dy + 2) * 256 }}
        src={`https://cyberjapandata.gsi.go.jp/xyz/seamlessphoto/${z}/${tx + dx}/${ty + dy}.jpg`} />);
  return (
    <>
      <div className="tiles" style={{ transform: `translate(${shiftX}px, ${shiftY}px) scale(1)` }}>{tiles}</div>
      <span className="ring" />
      <span className="cross" />
    </>
  );
}
