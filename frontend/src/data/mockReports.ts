export type ReportKind = "Social" | "Radio";
export type ReportStatus = "Processed" | "Replay";

/** Public report fields used by the dashboard. Scoring metadata stays backend-only. */
export interface MockReport {
  id: string;
  kind: ReportKind;
  time: string;
  status: ReportStatus;
  text: string;
  location: string;
  verified: boolean;
  incidentId: string;
  /** Real reports only: the language it was sent in, and its text in that language when it is not English. */
  lang?: string;
  originalText?: string;
  /** Real reports only: the report's own id, how it arrived, and where the map can find it. */
  observationId?: string;
  channel?: string;
  precision?: string;
  reliability?: number;
  longitude?: number;
  latitude?: number;
}
