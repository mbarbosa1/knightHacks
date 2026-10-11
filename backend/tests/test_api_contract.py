import unittest
from uuid import uuid4

from fastapi import HTTPException
from pydantic import ValidationError

from app.config import Settings
from app.main import create_app
from app.models import IncidentPut, ObservationCreate, Point, ResourcePut, RoadRestrictionPut, RunEnd
from app.repository import observation_matches, parse_bbox


class ContractTests(unittest.TestCase):
    def test_openapi_includes_new_routes(self):
        paths = create_app(Settings(database_url="postgresql://placeholder")).openapi()["paths"]
        for path, method in (
            ("/map/versions/{version_id}/buildings", "get"),
            ("/map/versions/{version_id}/roads", "get"),
            ("/runs/{run_id}/end", "post"),
            ("/runs/{run_id}/resources/{resource_id}", "put"),
            ("/runs/{run_id}/incidents/{incident_id}", "put"),
            ("/runs/{run_id}/road-restrictions/{road_id}", "put"),
            ("/runs/{run_id}/decisions/{decision_id}", "put"),
            ("/runs/{run_id}/decisions", "get"),
        ):
            self.assertIn(method, paths[path])

    def test_bbox_valid_and_invalid(self):
        self.assertEqual(parse_bbox("139,35,140,36"), (139,35,140,36))
        self.assertIsNone(parse_bbox(None))
        for value in ("0,0,1", "1,0,0,1", "0,nan,1,1", "-181,0,0,1", "x,y,z,w"):
            with self.assertRaises(HTTPException) as raised:
                parse_bbox(value)
            self.assertEqual(raised.exception.status_code, 422)

    def test_coordinates_and_counts(self):
        with self.assertRaises(ValidationError):
            Point(longitude=181, latitude=35)
        with self.assertRaises(ValidationError):
            ResourcePut(type="ambulance", capacity=-1)
        with self.assertRaises(ValidationError):
            IncidentPut(person_groups=[{"group_id": "g1", "est_count": -1}])

    def test_status_validation(self):
        for model, body in ((RunEnd, {"status": "running"}),
                            (RoadRestrictionPut, {"status": "closed"}),
                            (ResourcePut, {"type": "ambulance", "status": "ready"})):
            with self.assertRaises(ValidationError):
                model(**body)

    def test_evidence_requires_uuid(self):
        with self.assertRaises(ValidationError):
            IncidentPut(evidence={"supports": ["not-a-uuid"]})

    def test_observation_retry_checks_every_field(self):
        body = ObservationCreate(id=uuid4(), channel="sensor", source_reliability=0.7,
                                 sim_time_s=0.1, location=Point(longitude=139.7,latitude=35.7))
        stored = body.model_dump(exclude={"id", "location"})
        stored.update(longitude=139.7, latitude=35.7)
        self.assertTrue(observation_matches(stored, body))
        for field, changed in (("media_url", "https://example.org/changed"),
                               ("sim_time_s", 2), ("source_reliability", 0.2),
                               ("longitude", 139.8), ("claimed_location", "changed")):
            self.assertFalse(observation_matches({**stored, field: changed}, body), field)


if __name__ == "__main__":
    unittest.main()
