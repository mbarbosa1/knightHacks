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
}

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
  },
];
