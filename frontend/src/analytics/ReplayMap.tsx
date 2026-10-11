import { useEffect, useRef, useState } from "react";
import maplibregl, { type GeoJSONSource } from "maplibre-gl";
import "maplibre-gl/dist/maplibre-gl.css";
import { number, object, text, type Row } from "./data";

function features(reports: Row[], incidents: Row[]): GeoJSON.FeatureCollection {
  const output: GeoJSON.Feature[] = [];
  for (const [kind, rows] of [["report", reports], ["incident", incidents]] as const) {
    for (const row of rows) {
      const loc = object(row.location);
      const coordinates = Array.isArray(loc.coordinates) ? loc.coordinates : [row.longitude, row.latitude];
      const lon = number(coordinates[0]), lat = number(coordinates[1]);
      if (lon === null || lat === null || Math.abs(lon) > 180 || Math.abs(lat) > 90) continue;
      output.push({ type: "Feature", geometry: { type: "Point", coordinates: [lon, lat] },
        properties: { id: text(row.id), kind, description: text(row.text_en ?? row.original_text ?? row.status) } });
    }
  }
  return { type: "FeatureCollection", features: output };
}

export default function ReplayMap({ reports, incidents, focused }: { reports: Row[]; incidents: Row[]; focused: Row | null }) {
  const container = useRef<HTMLDivElement>(null);
  const map = useRef<maplibregl.Map | null>(null);
  const [ready, setReady] = useState(false);
  const [error, setError] = useState("");
  useEffect(() => {
    if (!container.current) return;
    let instance: maplibregl.Map;
    try {
      instance = new maplibregl.Map({ container: container.current, center: [139.7034, 35.6938], zoom: 13.5,
        style: { version: 8, sources: { osm: { type: "raster", tiles: ["https://tile.openstreetmap.org/{z}/{x}/{y}.png"], tileSize: 256, attribution: '© <a href="https://www.openstreetmap.org/copyright">OpenStreetMap</a> contributors' } },
          layers: [{ id: "basemap", type: "raster", source: "osm", paint: { "raster-saturation": -0.7 } }] } });
      map.current = instance;
      instance.addControl(new maplibregl.NavigationControl({ showCompass: false }), "top-right");
      instance.on("load", () => {
        instance.addSource("records", { type: "geojson", data: { type: "FeatureCollection", features: [] } });
        instance.addLayer({ id: "records", type: "circle", source: "records", paint: {
          "circle-color": ["match", ["get", "kind"], "incident", "#c8102e", "#376c85"],
          "circle-radius": ["match", ["get", "kind"], "incident", 7, 4], "circle-stroke-width": 1.5, "circle-stroke-color": "#fff", "circle-opacity": 0.85 } });
        instance.on("click", "records", e => {
          const f = e.features?.[0]; if (!f || f.geometry.type !== "Point") return;
          const content = document.createElement("div");
          const title = document.createElement("strong"); title.textContent = `${f.properties.kind} · ${String(f.properties.id).slice(0, 8)}`;
          const body = document.createElement("p"); body.textContent = f.properties.description;
          content.append(title, body);
          new maplibregl.Popup().setLngLat(f.geometry.coordinates as [number, number]).setDOMContent(content).addTo(instance);
        });
        setReady(true);
      });
    } catch { setError("This browser cannot render the map. The data explorer is still available."); return; }
    const resize = new ResizeObserver(() => instance.resize()); resize.observe(container.current);
    return () => { resize.disconnect(); instance.remove(); map.current = null; };
  }, []);
  useEffect(() => { if (ready) (map.current?.getSource("records") as GeoJSONSource | undefined)?.setData(features(reports, incidents)); }, [ready, reports, incidents]);
  useEffect(() => {
    if (!ready || !focused) return;
    const f = features([focused], []).features[0];
    if (f?.geometry.type === "Point") map.current?.flyTo({ center: f.geometry.coordinates as [number, number], zoom: 16 });
  }, [ready, focused]);
  const plotted = features(reports, incidents).features.length;
  return <div className="analytics-map-wrap">
    <div ref={container} className="analytics-map" aria-label="Map of recorded report and incident locations" />
    {error ? <p role="alert">{error}</p> : null}
    <div className="analytics-map-caption"><span><i className="legend-report" /> Reports <i className="legend-incident" /> Agent incidents</span><span>{plotted} recorded locations</span></div>
    <p className="analytics-caption">Source: Databricks operational history. Locations are reported positions; simulation truth tables do not include map geometry.</p>
  </div>;
}
