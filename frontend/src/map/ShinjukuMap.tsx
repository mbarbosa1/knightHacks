import { forwardRef, useEffect, useImperativeHandle, useRef, useState } from "react";
import maplibregl, { type GeoJSONSource, type Map as MapLibreMap } from "maplibre-gl";
import "maplibre-gl/dist/maplibre-gl.css";
import {
  EMPTY_OPERATIONAL,
  MAGNITUDE_RANGE,
  createEarthquake,
  simulatorAvailable,
  loadOperationalState,
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
const UNIT_COLORS = ["match", ["get", "type"], "ambulance", "#0c8599", "fire_engine", "#e8590c", "#5f3dc4"];

const empty = (): GeoJSON.FeatureCollection => ({ type: "FeatureCollection", features: [] });
const escapeHtml = (value: unknown) =>
  String(value ?? "").replace(/[&<>"]/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" })[c] ?? c);

interface Props {
  /** Draw every road segment, not only the affected ones. A debugging aid; off by default. */
  showRoadGraph?: boolean;
}

/**
 * The 3D map of Shinjuku.
 *  - Ground truth view: what really happened (operator's reveal-truth only).
 *  - Operational view: what agents believe (incidents, road restrictions, units). A placeholder until
 *    runs are written to the database; the layers exist and fill in when data arrives.
 */
export const ShinjukuMap = forwardRef<ShinjukuMapHandle, Props>(function ShinjukuMap({ showRoadGraph = false }, ref) {
  const container = useRef<HTMLDivElement>(null);
  const mapRef = useRef<MapLibreMap | null>(null);
  const staticRef = useRef<StaticMap | null>(null);
  const [ready, setReady] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [truth, setTruth] = useState<RunTruth | null>(null);
  const [operational, setOperational] = useState<OperationalState>(EMPTY_OPERATIONAL);
  const [view, setView] = useState<MapView>("truth");
  const [simulator, setSimulator] = useState(false);
  const [magnitude, setMagnitude] = useState(7.0);
  const [randomMagnitude, setRandomMagnitude] = useState(false);
  const [working, setWorking] = useState(false);
  const [quakeError, setQuakeError] = useState<string | null>(null);

  useEffect(() => {
    simulatorAvailable().then(setSimulator);
  }, []);

  const newEarthquake = async () => {
    setWorking(true);
    setQuakeError(null);
    try {
      setTruth(await createEarthquake(randomMagnitude ? null : magnitude));   // shown in whichever view is selected
      setSimulator(true);
    } catch {
      setSimulator(await simulatorAvailable());
      setQuakeError("The simulator did not answer.");
    } finally {
      setWorking(false);
    }
  };

  useImperativeHandle(ref, () => ({
    zoomIn: () => mapRef.current?.zoomIn(),
    zoomOut: () => mapRef.current?.zoomOut(),
    recenter: () => mapRef.current?.easeTo(HOME),
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
          paint: { "line-color": "#1c7ed6", "line-width": 4, "line-opacity": 0.85 },
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
          paint: { "circle-radius": 7, "circle-color": UNIT_COLORS as never, "circle-stroke-color": "#ffffff", "circle-stroke-width": 2 },
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
    if (!operationalConnected) return;
    let stop = false;
    const tick = () =>
      loadOperationalState()
        .then((state) => !stop && setOperational(state))
        .catch(() => undefined);
    tick();
    const timer = window.setInterval(tick, OPERATIONAL_REFRESH_MS);
    return () => {
      stop = true;
      window.clearInterval(timer);
    };
  }, []);

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
    (map.getSource("units") as GeoJSONSource).setData({
      type: "FeatureCollection",
      features: (showOps ? operational.resources : [])
        .filter((unit) => unit.location)
        .map((unit) => ({ type: "Feature", geometry: unit.location!, properties: { id: unit.id, type: unit.type, name: unit.name } })),
    });
  }, [view, truth, operational, ready]);

  const operationalEmpty =
    operational.incidents.length + operational.roadRestrictions.length + operational.resources.length === 0;

  const controlsOff = !simulator || working || !ready || Boolean(error);

  return (
    <>
      <div ref={container} className="map-canvas" />

      {/* One bar for everything about the run: which view, what happened, and making a new earthquake. */}
      <div className="map-bar">
        <div className="map-bar-row">
          <div className="map-view-switch" role="group" aria-label="Map view">
            <button className={view === "truth" ? "active" : ""} onClick={() => setView("truth")}>
              Ground truth
            </button>
            <button className={view === "operational" ? "active" : ""} onClick={() => setView("operational")}>
              Operational
            </button>
          </div>
          <div className="map-quake">
            <label htmlFor="quake-magnitude">Magnitude {randomMagnitude ? "random" : magnitude.toFixed(1)}</label>
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
            <button onClick={newEarthquake} disabled={controlsOff}>
              {working ? "Simulating…" : "Generate earthquake"}
            </button>
            <button
              className="map-quake-reset"
              onClick={() => {
                setTruth(null);
                setQuakeError(null);
              }}
              disabled={!truth || working}
            >
              Reset
            </button>
          </div>
        </div>
        <div className="map-status" aria-live="polite">
          {error ? (
            <span className="map-status-error">
              Map data could not be loaded ({error}). Run <code>python -m sim.export --seed 42</code> in <code>simulation/</code>.
            </span>
          ) : !ready ? (
            "Loading Shinjuku…"
          ) : quakeError ? (
            <span className="map-status-error">{quakeError}</span>
          ) : !truth ? (
            simulator ? (
              <>
                <b>Shinjuku, before the earthquake.</b> Generate one to see what happens. Each is new: depth, epicentre and
                which buildings fail are random.
              </>
            ) : (
              <>
                <b>Shinjuku, before the earthquake.</b> The simulator is not running, so earthquakes cannot be generated.
                Start everything with <code>./dev.sh</code>.
              </>
            )
          ) : view === "truth" ? (
            <>
              <b>
                Magnitude {truth.scenario.mw} · intensity {truth.intensity.label} (Japan scale)
              </b>{" "}
              · {truth.summary.buildings.collapsed ?? 0} collapsed · {truth.summary.buildings.severe ?? 0} severe ·{" "}
              {truth.summary.roads.BLOCKED ?? 0} roads blocked · {truth.summary.people.trapped ?? 0} trapped · seed {truth.seed}
            </>
          ) : operationalEmpty ? (
            <>
              <b>Operational view.</b>{" "}
              {operationalConnected
                ? "No incidents, closures or units yet."
                : "Not connected to a run yet: incidents, believed road closures and units appear here once runs are written to the database."}
            </>
          ) : (
            <>
              <b>Operational view.</b> {operational.incidents.length} incidents · {operational.roadRestrictions.length} road
              restrictions · {operational.resources.length} units
            </>
          )}
        </div>
      </div>

      {/* The key only appears once there is something to explain. */}
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
