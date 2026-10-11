from typing import Annotated, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

'''
Checks that the incoming JSON is valid
 
app/models.py	Defines what a valid request body looks like (for example, a run needs a seed and a valid hazard type). Bad input gets rejected 
before it reaches the database
'''
# Postgres REAL columns store finite float32 values.
Nonnegative = Annotated[float, Field(ge=0, le=3.402823466e38, allow_inf_nan=False)]
Identifier = Annotated[str, Field(min_length=1, max_length=200)]


class InputModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class Point(InputModel):
    longitude: float = Field(ge=-180, le=180, allow_inf_nan=False)
    latitude: float = Field(ge=-90, le=90, allow_inf_nan=False)


class RunCreate(InputModel):
    id: UUID | None = None
    map_version_id: Identifier
    seed: int = Field(ge=-(2**63), le=2**63 - 1)
    hazard_types: list[Literal["earthquake", "flood"]] = Field(min_length=1)


class ObservationCreate(InputModel):
    # Optional; a stable simulator-chosen UUID makes retried submissions idempotent.
    id: UUID | None = None
    channel: Literal[
        "emergency_call_119", "eyewitness_text", "social_media", "fire_police_radio",
        "hospital_feed", "shelter_update", "ward_bulletin", "sensor", "responder_update",
    ]
    format: Literal["audio", "text", "structured"] | None = None
    original_text: str | None = Field(default=None, max_length=20000)
    lang: str | None = Field(default=None, max_length=40)
    text_en: str | None = Field(default=None, max_length=20000)
    media_url: str | None = Field(default=None, max_length=2000)
    claimed_location: str | None = Field(default=None, max_length=2000)
    location: Point | None = None
    location_precision: Literal["gps", "address", "landmark", "vague"] | None = None
    source_reliability: float | None = Field(default=None, ge=0, le=1, allow_inf_nan=False)
    sim_time_s: Nonnegative | None = None


class RunEnd(InputModel):
    status: Literal["completed", "failed"] = "completed"
    sim_time_s: Nonnegative | None = None


class ResourcePut(InputModel):
    type: Identifier
    name: str | None = Field(default=None, max_length=200)
    location: Point | None = None
    home_facility_id: Identifier | None = None
    status: Literal["available", "en_route", "busy", "out_of_service"] = "available"
    capacity: int | None = Field(default=None, ge=0, le=2**31-1)
    skills: list[Identifier] | None = None
    sim_time_s: Nonnegative | None = None


class PersonGroup(InputModel):
    group_id: Identifier
    est_count: int = Field(ge=0, le=2**31-1)
    condition: Identifier | None = None
    needs: list[Identifier] = Field(default_factory=list)
    confidence: float | None = Field(default=None, ge=0, le=1, allow_inf_nan=False)


class IncidentEvidence(InputModel):
    supports: list[UUID] = Field(default_factory=list)
    contradicts: list[UUID] = Field(default_factory=list)


class IncidentPut(InputModel):
    location: Point | None = None
    uncertainty_m: Nonnegative | None = None
    status: Literal["reported", "corroborated", "disputed", "resolved"] = "reported"
    building_id: Identifier | None = None
    person_groups: list[PersonGroup] | None = None
    evidence: IncidentEvidence = Field(default_factory=IncidentEvidence)
    sim_time_s: Nonnegative | None = None


class RoadRestrictionPut(InputModel):
    status: Literal["OPEN", "LIMITED", "BLOCKED"]
    source_observation_id: UUID | None = None
    sim_time_s: Nonnegative | None = None


class IngestObservation(ObservationCreate):
    id: UUID


class InitialResource(ResourcePut):
    id: UUID


class SimulationIngest(InputModel):
    observations: list[IngestObservation] = Field(max_length=10000)
    resources: list[InitialResource] = Field(default_factory=list, max_length=1000)
