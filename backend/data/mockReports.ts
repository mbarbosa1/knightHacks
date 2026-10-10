export type ReportKind = "Social" | "Radio";
export type ReportStatus = "Processed" | "Replay";
export type ReportNoise =
  | "clean"
  | "duplicate"
  | "wrong_count"
  | "vague_location"
  | "contradiction"
  | "rumor";

export interface MockReport {
  id: string;
  kind: ReportKind;
  time: string;
  status: ReportStatus;
  text: string;
  location: string;
  verified: boolean;
  incidentId: string;
  noise: ReportNoise;
  /** Scoring-only truth link. Never render this field in the operator UI. */
  answerKeyTruthEventId: string | null;
}

export interface MockTruthEvent {
  id: string;
  incidentId: string;
  summary: string;
}

export const mockTruthEvents: MockTruthEvent[] = [
  {
    id: "TRUTH-042-STRUCTURE",
    incidentId: "INC-042",
    summary: "Shinjuku Ward Office has structural damage and people may be trapped.",
  },
  {
    id: "TRUTH-038-COLLAPSE",
    incidentId: "INC-038",
    summary: "Residencias Caribe has a partial collapse and residents need assistance.",
  },
  {
    id: "TRUTH-038-ROAD",
    incidentId: "INC-038",
    summary: "Avenida Principal is blocked near Residencias Caribe.",
  },
  {
    id: "TRUTH-031-ROAD",
    incidentId: "INC-031",
    summary: "Debris obstructs the north entrance near Plaza Bolívar.",
  },
  {
    id: "TRUTH-044-MEDICAL",
    incidentId: "INC-044",
    summary: "A resident at the west clinic needs urgent medical assistance.",
  },
  {
    id: "TRUTH-050-RUMOR-CHECK",
    incidentId: "INC-042",
    summary: "No structural collapse was found at the old market; the rumor was false.",
  },
];

// The feed is intentionally noisy. Unreported truth events model missed events.
export const mockReports: MockReport[] = [
  {
    id: "R-1042",
    kind: "Social",
    time: "10:03",
    status: "Processed",
    text: "Someone said children may be trapped at Shinjuku Mayor's Office. I didn’t see it myself.",
    location: "Colegio Tanagueranas",
    verified: true,
    incidentId: "INC-042",
    noise: "vague_location",
    answerKeyTruthEventId: "TRUTH-042-STRUCTURE",
  },
  {
    id: "R-1041",
    kind: "Social",
    time: "10:02",
    status: "Replay",
    text: "Forwarding this again: people are trapped in the Shinjuku Ward Office. Anyone nearby?",
    location: "Shinjuku",
    verified: false,
    incidentId: "INC-042",
    noise: "duplicate",
    answerKeyTruthEventId: "TRUTH-042-STRUCTURE",
  },
  {
    id: "R-1040",
    kind: "Radio",
    time: "10:00",
    status: "Replay",
    text: "Command, Rescue Fire Department: partial collapse at Residencias Caribe. Avenida Principal is blocked.",
    location: "Avenida Principal",
    verified: false,
    incidentId: "INC-038",
    noise: "clean",
    answerKeyTruthEventId: "TRUTH-038-COLLAPSE",
  },
  {
    id: "R-1039",
    kind: "Social",
    time: "09:58",
    status: "Processed",
    text: "Road is clear up to the plaza. Debris is covering the north entrance.",
    location: "Plaza Bolívar",
    verified: false,
    incidentId: "INC-031",
    noise: "contradiction",
    answerKeyTruthEventId: "TRUTH-031-ROAD",
  },
  {
    id: "R-1038",
    kind: "Radio",
    time: "09:56",
    status: "Processed",
    text: "Multiple people trapped at Residencias Caribe—maybe six, hard to confirm from here.",
    location: "Near the main avenue",
    verified: false,
    incidentId: "INC-038",
    noise: "wrong_count",
    answerKeyTruthEventId: "TRUTH-038-COLLAPSE",
  },
  {
    id: "R-1037",
    kind: "Social",
    time: "09:53",
    status: "Replay",
    text: "Heard the old market collapsed. No one I know has been able to confirm it.",
    location: "Old market, maybe by the waterfront",
    verified: false,
    incidentId: "INC-042",
    noise: "rumor",
    answerKeyTruthEventId: null,
  },
];

// TRUTH-038-ROAD and TRUTH-044-MEDICAL intentionally have no matching report.
