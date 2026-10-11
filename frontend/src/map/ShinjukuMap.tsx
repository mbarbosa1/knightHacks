import { forwardRef, useEffect, useImperativeHandle, useRef, useState } from "react";
import maplibregl, { type GeoJSONSource, type Map as MapLibreMap } from "maplibre-gl";
import "maplibre-gl/dist/maplibre-gl.css";
import {
  EMPTY_OPERATIONAL,
  MAGNITUDE_RANGE,
  createEarthquake,
  retryEarthquake,
  forgetRun,
  DEFAULT_SPEED,
  SPEEDS,
  changeClock,
  loadClock,
  loadUnits,
  type LiveUnit,
  stopRun,
  type RunClock,
  SubmissionError,
  RUN_ID,
  simulatorAvailable,
  loadOperationalState,
  loadReportsSince,
  type Report,
  loadRunTruth,
  loadStaticMap,
  operationalConnected,
  type DamageLevel,
  type OperationalState,
  type RunTruth,
  type StaticMap,
} from "./dataSource";

export type MapView = "truth" | "operational";

export interface ShinjukuMapHandle {
  zoomIn(): void;
  zoomOut(): void;
  recenter(): void;
  /** Fly to a place, e.g. where a report says it is. */
  focus(lon: number, lat: number): void;
  /** What to call a building in the side panels: its name, else its address, else what it is used for. */
  buildingName(id: string): string | null;
}

/** What the map knows, handed to the dashboard so its side panels can show it. The map itself stays clean. */
export interface MapState {
  view: MapView;
  truth: RunTruth | null;
  clock: RunClock | null;
  operational: OperationalState;
  reports: Report[];
  units: LiveUnit[];
}

const CENTER: [number, number] = [139.7034, 35.6938]; // Shinjuku Station
const HOME = { center: CENTER, zoom: 14.6, pitch: 55, bearing: -20 };
const OPERATIONAL_REFRESH_MS = 3000;

export const DAMAGE_LEVELS: DamageLevel[] = ["none", "light", "moderate", "severe", "collapsed"];
export const DAMAGE_COLORS: Record<DamageLevel, string> = {
  none: "#c5ced4",
  light: "#f0d264",
  moderate: "#f0a03c",
  severe: "#e0562c",
  collapsed: "#8c1a14",
};
export const ROAD_COLORS = { BLOCKED: "#d81e2c", LIMITED: "#f09a1a" } as const;
export const FACILITY_MARK = {
  hospital: ["H", "#d6336c", "hospital"],
  fire_station: ["F", "#e8590c", "fire station"],
  police: ["P", "#3b5bdb", "police"],
  shelter: ["S", "#2b8a3e", "shelter"],
} as const;
/** One colour per way a report can arrive. */
export const CHANNELS: Record<string, [label: string, color: string]> = {
  emergency_call_119: ["119 call", "#d81e2c"],
  eyewitness_text: ["text message", "#1c7ed6"],
  social_media: ["social media", "#7048e8"],
  fire_police_radio: ["fire / police radio", "#2b8a3e"],
  responder_update: ["unit on the ground", "#e8590c"],
};
const REPORT_FRESH_MS = 5000;   // how long a new report stays highlighted on the map
const REPORTS_REFRESH_MS = 2000;
export const UNIT_COLOR = { fire_engine: "#e8590c", ambulance: "#0c8599", police: "#5f3dc4" } as const;
export const UNIT_LABEL = { fire_engine: "fire engine", ambulance: "ambulance", police: "police patrol" } as const;
const UNIT_COLORS = ["match", ["get", "type"], "ambulance", UNIT_COLOR.ambulance, "fire_engine", UNIT_COLOR.fire_engine, UNIT_COLOR.police];
const UNIT_LETTER = { fire_engine: "E", ambulance: "A", police: "P" } as const;
const UNIT_STATUS = { en_route: "on the way", busy: "working on scene", available: "free" } as const;
const UNITS_REFRESH_MS = 500;

export const formatClock = (s: number) =>
  `${String(Math.floor(s / 60)).padStart(2, "0")}:${String(Math.floor(s % 60)).padStart(2, "0")}`;
const empty = (): GeoJSON.FeatureCollection => ({ type: "FeatureCollection", features: [] });
const escapeHtml = (value: unknown) =>
  String(value ?? "").replace(/[&<>"]/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" })[c] ?? c);

interface Props {
  /** Draw every road segment, not only the affected ones. A debugging aid; off by default. */
  showRoadGraph?: boolean;
  /** Called whenever anything the side panels show has changed. */
  onUpdate?: (state: MapState) => void;

}

/**
 * The 3D map of Shinjuku.
 *  - Ground truth view: what really happened (operator's reveal-truth only).
 *  - Operational view: what agents believe (incidents, road restrictions, units). A placeholder until
 *    runs are written to the database; the layers exist and fill in when data arrives.
 */
export const ShinjukuMap = forwardRef<ShinjukuMapHandle, Props>(function ShinjukuMap({ showRoadGraph = false, onUpdate }, ref) {

  const container = useRef<HTMLDivElement>(null);
  const mapRef = useRef<MapLibreMap | null>(null);
  const staticRef = useRef<StaticMap | null>(null);
  const unitMarkers = useRef(new Map<string, maplibregl.Marker>());
  const [ready, setReady] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [truth, setTruth] = useState<RunTruth | null>(null);
  const [operational, setOperational] = useState<OperationalState>(EMPTY_OPERATIONAL);
  const [view, setView] = useState<MapView>("operational");
  // Reports for the run being followed, with the moment this page first saw each one.
  const [reports, setReports] = useState<(Report & { seenAt: number })[]>([]);
  const [, setPulse] = useState(0);
  const [simulator, setSimulator] = useState(false);
  const [magnitude, setMagnitude] = useState(7.0);
  const [randomMagnitude, setRandomMagnitude] = useState(false);
  const [working, setWorking] = useState(false);
  const [quakeError, setQuakeError] = useState<string | null>(null);
  const [retryRunId, setRetryRunId] = useState<string | null>(null);
  const [speed, setSpeed] = useState<number>(DEFAULT_SPEED);
  const [clock, setClock] = useState<RunClock | null>(null);
  const [units, setUnits] = useState<LiveUnit[]>([]);

  // The simulator keeps the clock; the map only asks what time it is.
  useEffect(() => {
    if (!truth) {
      setClock(null);
      return;
    }
    let stop = false;
    const tick = () => loadClock().then((c) => !stop && setClock(c && c.run_id === truth.run_id ? c : null));
    tick();
    const timer = window.setInterval(tick, 1000);
    return () => {
      stop = true;
      window.clearInterval(timer);
    };
  }, [truth]);

  // The simulator moves the units; the map asks where they are twice a second while a run is on screen.
  useEffect(() => {
    setUnits([]);
    if (!truth) return;
    let stop = false;
    const tick = () => loadUnits().then((u) => !stop && setUnits(u));
    tick();
    const timer = window.setInterval(tick, UNITS_REFRESH_MS);
    return () => {
      stop = true;
      window.clearInterval(timer);
    };
  }, [truth]);
  // After Reset the map follows no run at all, not the one this tab remembered from before.
  const [detached, setDetached] = useState(false);
  const activeRunId = truth?.ingestion?.ready ? truth.run_id : detached ? undefined : RUN_ID;

  useEffect(() => {
    simulatorAvailable().then(setSimulator);
  }, []);

  const newEarthquake = async (retryId?: string) => {
    setWorking(true);
    setQuakeError(null);
    try {
      setTruth(await (retryId ? retryEarthquake(retryId) : createEarthquake(randomMagnitude ? null : magnitude, speed)));
      setRetryRunId(null);
      setDetached(false);
      setSimulator(true);
    } catch (cause) {
      setSimulator(await simulatorAvailable());
      setRetryRunId(cause instanceof SubmissionError && cause.retryable ? cause.runId : null);
      setQuakeError(cause instanceof Error ? cause.message : "The simulator did not answer. Saving has not been confirmed.");
    } finally {
      setWorking(false);
    }
  };

  useImperativeHandle(ref, () => ({
    zoomIn: () => mapRef.current?.zoomIn(),
    zoomOut: () => mapRef.current?.zoomOut(),
    recenter: () => mapRef.current?.easeTo(HOME),
    focus: (lon, lat) => mapRef.current?.easeTo({ center: [lon, lat], zoom: 16.5, duration: 700 }),
    buildingName: (id) => {
      const p = staticRef.current?.buildings.features.find((f) => f.properties.id === id)?.properties;
      return p ? p.name_en || p.address_en?.split(",")[0] || (p.usage ? p.usage.replace(/_/g, " ") : null) : null;
    },
  }));

  // Build the map once: basemap, static layers, and empty layers for everything that arrives later.
  useEffect(() => {
    if (!container.current) return;
    let cancelled = false;
    const map = new maplibregl.Map({
      container: container.current,
      style: "https://tiles.openfreemap.org/styles/positron",
      ...HOME,
      attributionControl: false, // credits are in the dashboard's own attribution strip
    });
    mapRef.current = map;
    const popup = new maplibregl.Popup({ closeButton: false, closeOnClick: false, className: "map-tip", offset: 10 });
    const markers: maplibregl.Marker[] = [];

    map.on("load", async () => {
      try {
        const [staticMap, runTruth] = await Promise.all([loadStaticMap(), loadRunTruth()]);
        if (cancelled) return;
        staticRef.current = staticMap;

        // The basemap draws its own flat buildings; ours replace them.
        for (const layer of map.getStyle().layers ?? []) {
          if (layer.id.includes("building")) map.removeLayer(layer.id);
        }

        map.addSource("roads", { type: "geojson", data: staticMap.roads, promoteId: "id" });
        map.addLayer({
          id: "road-graph",
          type: "line",
          source: "roads",
          layout: { visibility: "none" },
          paint: { "line-color": "#7b8a94", "line-width": 1, "line-opacity": 0.7 },
        });
        map.addLayer({
          id: "road-status",
          type: "line",
          source: "roads",
          layout: { "line-cap": "round" },
          paint: {
            "line-color": ["match", ["feature-state", "status"], "BLOCKED", ROAD_COLORS.BLOCKED, "LIMITED", ROAD_COLORS.LIMITED, "rgba(0,0,0,0)"],
            "line-width": ["match", ["feature-state", "status"], "BLOCKED", 6, "LIMITED", 4, 0],
          },
        });

        // Filled by the live loop and the agents later; empty for now.
        map.addSource("routes", { type: "geojson", data: empty() });
        map.addLayer({
          id: "routes",
          type: "line",
          source: "routes",
          layout: { "line-cap": "round", "line-join": "round" },
          paint: { "line-color": ["get", "color"], "line-width": 3, "line-opacity": 0.7, "line-dasharray": [1, 1.5] },
        });

        map.addSource("buildings", { type: "geojson", data: staticMap.buildings, promoteId: "id" });
        map.addLayer({
          id: "buildings",
          type: "fill-extrusion",
          source: "buildings",
          paint: {
            "fill-extrusion-color": [
              "match", ["coalesce", ["feature-state", "damage"], "none"],
              "light", DAMAGE_COLORS.light, "moderate", DAMAGE_COLORS.moderate,
              "severe", DAMAGE_COLORS.severe, "collapsed", DAMAGE_COLORS.collapsed, DAMAGE_COLORS.none,
            ],
            // A collapsed building is drawn as a low pile; unknown heights get a 6 m stand-in.
            "fill-extrusion-height": [
              "*", ["coalesce", ["get", "height_m"], 6],
              ["case", ["==", ["feature-state", "damage"], "collapsed"], 0.25, 1],
            ],
            "fill-extrusion-opacity": 0.92,
          },
        });

        // Reports: a dot where each caller says they are. New ones are larger for a few seconds.
        map.addSource("reports", { type: "geojson", data: empty() });
        map.addLayer({
          id: "reports",
          type: "circle",
          source: "reports",
          paint: {
            "circle-radius": ["case", ["get", "fresh"], 15, 7],
            "circle-color": ["get", "color"],
            "circle-opacity": ["case", ["get", "fresh"], 0.35, 0.95],
            "circle-stroke-color": ["case", ["get", "fresh"], ["get", "color"], "#ffffff"],
            "circle-stroke-width": ["case", ["get", "fresh"], 3, 2],
          },
        });

        map.addSource("incidents", { type: "geojson", data: empty() });
        map.addLayer({
          id: "incidents",
          type: "circle",
          source: "incidents",
          paint: { "circle-radius": 9, "circle-color": "rgba(216,30,44,0.2)", "circle-stroke-color": "#d81e2c", "circle-stroke-width": 2 },
        });
        map.addSource("units", { type: "geojson", data: empty() });
        map.addLayer({
          id: "units",
          type: "circle",
          source: "units",
          paint: {
            // Driving: a solid dot. Working on scene: a dark ring around it. Free again after a job: faded.
            "circle-radius": ["match", ["get", "status"], "busy", 9, 7],
            "circle-color": UNIT_COLORS as never,
            "circle-opacity": ["match", ["get", "status"], "available", 0.45, 1],
            "circle-stroke-color": ["match", ["get", "status"], "busy", "#18212b", "#ffffff"],
            "circle-stroke-width": ["match", ["get", "status"], "busy", 3, 2],
            "circle-stroke-opacity": ["match", ["get", "status"], "available", 0.6, 1],
          },
        });

        for (const facility of staticMap.facilities) {
          // A small lettered pin; the name appears on hover or keyboard focus so labels don't cover the city.
          const [letter, color, kind] = FACILITY_MARK[facility.type] ?? ["?", "#495057", facility.type];
          const name = (facility.name_en ?? kind).replace(/ \(.*\)$/, "");
          const el = document.createElement("div");
          el.className = "map-facility";
          el.tabIndex = 0;
          el.setAttribute("aria-label", `${name} (${kind})`);
          el.style.setProperty("--facility", color);
          el.innerHTML = `<b>${letter}</b><span>${escapeHtml(name)}</span>`;
          markers.push(new maplibregl.Marker({ element: el, anchor: "center" }).setLngLat(facility.geometry.coordinates as [number, number]).addTo(map));
        }

        map.on("mousemove", "buildings", (event) => {
          const feature = event.features?.[0];
          if (!feature) return;
          const state = map.getFeatureState({ source: "buildings", id: feature.id as string });
          const p = feature.properties as Record<string, unknown>;
          const lines = [
            `<strong>${escapeHtml(p.name_en || String(p.usage ?? "building").replace(/_/g, " "))}</strong>`,
            `${escapeHtml(p.structure ?? "unknown structure")} · ${p.floors ?? "?"} floors · ${p.height_m ?? "?"} m`,
          ];
          if (state.view === "truth") {
            lines.push(`Damage: <b>${escapeHtml(state.damage ?? "none")}</b>${state.intensity ? ` · intensity ${state.intensity}` : ""}`);
            if (state.people) lines.push(escapeHtml(state.people));
          }
          popup.setLngLat(event.lngLat).setHTML(lines.join("<br>")).addTo(map);
        });
        map.on("mouseleave", "buildings", () => popup.remove());
        map.on("mousemove", "reports", (event) => {
          const p = event.features?.[0]?.properties as Record<string, unknown> | undefined;
          if (!p) return;
          popup
            .setLngLat(event.lngLat)
            .setHTML(
              `<strong>${escapeHtml(p.label)}</strong> · T+${escapeHtml(p.time)}<br>${escapeHtml(p.text)}` +
                (p.place ? `<br><em>${escapeHtml(p.place)}</em>` : ""),
            )
            .addTo(map);
        });
        map.on("mouseleave", "reports", () => popup.remove());

        setTruth(runTruth);
        setReady(true);
      } catch (cause) {
        if (!cancelled) setError(cause instanceof Error ? cause.message : String(cause));
      }
    });

    return () => {
      cancelled = true;
      markers.forEach((marker) => marker.remove());
      popup.remove();
      map.remove();
      mapRef.current = null;
    };
  }, []);

  // Operational state: polled while a run is connected, otherwise empty.
  useEffect(() => {
    setOperational(EMPTY_OPERATIONAL);   // never keep showing the previous run's units
    if (!operationalConnected || !activeRunId) return;
    let stop = false;
    const tick = () =>
      loadOperationalState(activeRunId)
        .then((state) => !stop && setOperational(state))
        .catch(() => undefined);
    tick();
    const timer = window.setInterval(tick, OPERATIONAL_REFRESH_MS);
    return () => {
      stop = true;
      window.clearInterval(timer);
    };
  }, [activeRunId]);

  // Reports for the run being followed, fetched as they arrive.
  useEffect(() => {
    setReports([]);
    if (!operationalConnected || !activeRunId) return;
    let stop = false;
    let cursor: string | undefined;
    let busy = false;
    const tick = async () => {
      if (busy) return;
      busy = true;
      try {
        const fresh = await loadReportsSince(activeRunId, cursor);
        if (!stop && fresh.length) {
          cursor = fresh[fresh.length - 1].id;
          const now = Date.now();
          setReports((old) => [...old, ...fresh.map((r) => ({ ...r, seenAt: now }))]);
        }
      } catch {
        // the next poll tries again from the same place
      } finally {
        busy = false;
      }
    };
    tick();
    const timer = window.setInterval(tick, REPORTS_REFRESH_MS);
    const pulse = window.setInterval(() => setPulse((n) => n + 1), 1000);   // lets highlights fade
    return () => {
      stop = true;
      window.clearInterval(timer);
      window.clearInterval(pulse);
    };
  }, [activeRunId]);

  // A dot where each caller says they are. Shown in both views: a report is not hidden truth.
  useEffect(() => {
    const map = mapRef.current;
    if (!map || !ready) return;
    const now = Date.now();
    (map.getSource("reports") as GeoJSONSource).setData({
      type: "FeatureCollection",
      features: reports
        .filter((r) => r.longitude !== null && r.latitude !== null)
        .map((r) => ({
          type: "Feature",
          geometry: { type: "Point", coordinates: [r.longitude!, r.latitude!] },
          properties: {
            color: CHANNELS[r.channel]?.[1] ?? "#495057",
            label: CHANNELS[r.channel]?.[0] ?? r.channel,
            fresh: now - r.seenAt < REPORT_FRESH_MS,
            time: formatClock(r.sim_time_s ?? 0),
            text: r.text_en ?? r.original_text ?? "",
            place: r.claimed_location ?? "",
          },
        })),
    });
  });

  useEffect(() => {
    const map = mapRef.current;
    if (map && ready) map.setLayoutProperty("road-graph", "visibility", showRoadGraph ? "visible" : "none");
  }, [showRoadGraph, ready]);

  // Paint the chosen view. Buildings and roads carry it as feature state; points are GeoJSON sources.
  useEffect(() => {
    const map = mapRef.current;
    const staticMap = staticRef.current;
    if (!map || !ready || !staticMap) return;
    map.removeFeatureState({ source: "buildings" });
    map.removeFeatureState({ source: "roads" });
    for (const feature of staticMap.buildings.features) {
      map.setFeatureState({ source: "buildings", id: feature.properties.id }, { view });
    }

    if (view === "truth" && truth) {
      for (const [id, d] of Object.entries(truth.building_damage)) {
        const hurt = truth.people_by_building[id];
        const people = hurt ? Object.entries(hurt).map(([condition, n]) => `${n} ${condition}`).join(", ") : "";
        map.setFeatureState({ source: "buildings", id }, { view, damage: d.damage_level, intensity: d.intensity, people });
      }
      for (const [id, road] of Object.entries(truth.road_status)) {
        map.setFeatureState({ source: "roads", id }, { status: road.status });
      }
    }
    if (view === "operational") {
      for (const road of operational.roadRestrictions) {
        map.setFeatureState({ source: "roads", id: road.road_id }, { status: road.status });
      }
    }

    const showOps = view === "operational";
    (map.getSource("incidents") as GeoJSONSource).setData({
      type: "FeatureCollection",
      features: (showOps ? operational.incidents : [])
        .filter((incident) => incident.location)
        .map((incident) => ({ type: "Feature", geometry: incident.location!, properties: { id: incident.id, status: incident.status } })),
    });
  }, [view, truth, operational, ready]);

  // Units, in the operational view. One still parked at its station is not drawn: it is under the station's pin.
  useEffect(() => {
    const map = mapRef.current;
    if (!map || !ready) return;
    const shown = view === "operational" ? units.filter((unit) => unit.destination) : [];
    // A square lettered badge per unit, so it cannot be mistaken for a report dot or a station pin.
    const markers = unitMarkers.current;
    for (const [id, marker] of markers) {
      if (!shown.some((unit) => unit.id === id)) {
        marker.remove();
        markers.delete(id);
      }
    }
    for (const unit of shown) {
      let marker = markers.get(unit.id);
      if (!marker) {
        const el = document.createElement("div");
        el.style.setProperty("--unit", UNIT_COLOR[unit.type] ?? UNIT_COLOR.police);
        el.textContent = UNIT_LETTER[unit.type] ?? "?";
        marker = new maplibregl.Marker({ element: el, anchor: "center" }).setLngLat([unit.lon, unit.lat]).addTo(map);
        markers.set(unit.id, marker);
      }
      marker.setLngLat([unit.lon, unit.lat]);
      const el = marker.getElement();
      // Keep MapLibre's own classes on the element: they position it.
      el.classList.add("map-unit");
      el.classList.toggle("map-unit-busy", unit.status === "busy");
      el.classList.toggle("map-unit-free", unit.status === "available");
      el.title = `${unit.name}: ${UNIT_STATUS[unit.status] ?? unit.status}${unit.helped ? `, ${unit.helped} helped` : ""}`;
    }
    (map.getSource("routes") as GeoJSONSource).setData({
      type: "FeatureCollection",
      features: shown
        .filter((unit) => unit.status === "en_route" && unit.route)
        .map((unit) => ({
          type: "Feature",
          geometry: { type: "LineString", coordinates: unit.route! },
          properties: { id: unit.id, color: UNIT_COLOR[unit.type] ?? UNIT_COLOR.police },
        })),
    });
  }, [view, units, ready]);

  useEffect(() => {
    onUpdate?.({ view, truth, clock, operational, reports, units });
  }, [view, truth, clock, operational, reports, units, onUpdate]);


  // The bar carries no figures: counts and summaries live in the dashboard's side panels.
  const notice = error
    ? `Map data could not be loaded (${error}).`
    : !ready
      ? "Loading Shinjuku…"
      : quakeError
        ? quakeError
        : working
          ? "Generating the earthquake and saving it…"
          : !simulator
            ? "The simulator is not running. Start everything with ./dev.sh"
            : clock?.last_error
              ? "A report failed to save and is being retried."
              : null;

  const controlsOff = !simulator || working || !ready || Boolean(error);

  return (
    <>
      <div ref={container} className="map-canvas" />

      {/* One row of controls. Everything informational is in the dashboard's side panels. */}
      <div className="map-bar">
        <div className="map-view-switch" role="group" aria-label="Map view">
          <button className={view === "operational" ? "active" : ""} onClick={() => setView("operational")}>
            Operational
          </button>
          <button className={view === "truth" ? "active" : ""} onClick={() => setView("truth")}>
            Ground truth
          </button>
        </div>
        {truth && (
          <div className="map-clock" role="status">
            <b className="map-clock-time">T+{formatClock(clock?.sim_time_s ?? 0)}</b>
            <button
              className="map-clock-button"
              disabled={!clock || clock.finished}
              onClick={() => clock && changeClock({ playing: !clock.playing }).then((c) => c && setClock(c))}
            >
              {clock?.finished ? "Finished" : clock?.playing ? "Pause" : "Play"}
            </button>
            <div className="map-view-switch" role="group" aria-label="Simulation speed">
              {SPEEDS.map((x) => (
                <button
                  key={x}
                  className={(clock?.speed ?? speed) === x ? "active" : ""}
                  disabled={!clock || clock.finished}
                  onClick={() => {
                    setSpeed(x);
                    changeClock({ speed: x }).then((c) => c && setClock(c));
                  }}
                >
                  {x}x
                </button>
              ))}
            </div>
            {view === "operational" && clock?.rescued !== undefined && (
              <span className="map-clock-totals">
                <b>{clock.rescued}</b> rescued · <b>{clock.treated ?? 0}</b> treated
              </span>
            )}
          </div>
        )}
        <div className="map-quake">
          <label htmlFor="quake-magnitude">M {randomMagnitude ? "random" : magnitude.toFixed(1)}</label>
          <input
            id="quake-magnitude"
            type="range"
            min={MAGNITUDE_RANGE.min}
            max={MAGNITUDE_RANGE.max}
            step={MAGNITUDE_RANGE.step}
            value={magnitude}
            disabled={randomMagnitude || controlsOff}
            onChange={(event) => setMagnitude(Number(event.target.value))}
          />
          <label className="map-quake-random">
            <input
              id="quake-random"
              type="checkbox"
              checked={randomMagnitude}
              disabled={controlsOff}
              onChange={(event) => setRandomMagnitude(event.target.checked)}
            />
            random
          </label>
          <button onClick={() => newEarthquake()} disabled={controlsOff}>
            {working ? "Generating…" : "Generate"}
          </button>
          {retryRunId && (
            <button onClick={() => newEarthquake(retryRunId)} disabled={working}>
              Retry saving
            </button>
          )}
          <button
            className="map-quake-reset"
            onClick={() => {
              stopRun();   // one run plays at a time: Reset stops its clock and closes it
              setTruth(null);
              setQuakeError(null);
              setRetryRunId(null);
              setDetached(true);
              forgetRun();
            }}
            disabled={!truth || working}
          >
            Reset
          </button>
        </div>
        {notice && (
          <div className={`map-notice${error || quakeError ? " map-status-error" : ""}`} aria-live="polite">
            {notice}
          </div>
        )}
      </div>

      {/* The key only appears once there is something to explain. */}
      {view === "operational" && (reports.length > 0 || units.some((u) => u.destination)) && (
        <div className="map-legend damage-legend">
          {(Object.keys(UNIT_COLOR) as (keyof typeof UNIT_COLOR)[])
            .filter((type) => units.some((u) => u.destination && u.type === type))
            .map((type) => (
              <span key={type}>
                <i className="legend-unit" style={{ background: UNIT_COLOR[type] }}>{UNIT_LETTER[type]}</i> {UNIT_LABEL[type]}
              </span>
            ))}
          {Object.entries(CHANNELS)
            .filter(([channel]) => reports.some((r) => r.channel === channel))
            .map(([channel, [label, color]]) => (
              <span key={channel}>
                <i className="legend-dot" style={{ background: color }} /> {label}
              </span>
            ))}
        </div>
      )}
      {truth && view === "truth" && (
        <div className="map-legend damage-legend">
          {DAMAGE_LEVELS.map((level) => (
            <span key={level}>
              <i style={{ background: DAMAGE_COLORS[level] }} /> {level}
            </span>
          ))}
          <span>
            <i style={{ background: ROAD_COLORS.BLOCKED }} /> road blocked
          </span>
          <span>
            <i style={{ background: ROAD_COLORS.LIMITED }} /> road limited
          </span>
          {Object.values(FACILITY_MARK).map(([letter, color, kind]) => (
            <span key={kind}>
              <i className="legend-facility" style={{ background: color }}>
                {letter}
              </i>{" "}
              {kind}
            </span>
          ))}
        </div>
      )}
    </>
  );
});
