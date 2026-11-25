"""Regression tests for the protected, read-only Fibaro HC3 API."""

import http.client
import json
import threading
import unittest
from contextlib import closing
from unittest.mock import patch
from http.server import ThreadingHTTPServer

import api
from fibaro_hc3_api import build_hc3_snapshot


class FakeProvider:
    def __init__(self):
        self.zone_heads = {
            6: [2530, 2534],
            7: [2162, 2170],
            8: [2494],
            9: [2518],
            10: [2145, 2149],
            12: [2154],
            13: [2297, 2542, 2607],
            14: [2502],
            15: [2166],
            16: [2498],
        }
        self.zone_ids = sorted(self.zone_heads)
        self.head_ids = sorted({
            device_id for values in self.zone_heads.values() for device_id in values
        })

    def get_climate_zones(self):
        return [
            {
                "id": zone_id,
                "name": f"Zone {zone_id}",
                "active": True,
                "mode": "Schedule",
                "properties": {
                    "devices": self.zone_heads[zone_id],
                    "temperatureSensors": [247 + index],
                },
            }
            for index, zone_id in enumerate(self.zone_ids)
        ]

    def get_climate_zone(self, zone_id):
        item = next(zone for zone in self.get_climate_zones() if zone["id"] == zone_id)
        return {
            **item,
            "properties": {
                **item["properties"],
                "currentTemperature": 20.5,
                "currentTemperatureHeating": 21.5,
                "calculationStrategy": "avg",
                "schedule": {"private": "not exposed"},
            },
        }

    def get_events_range(self, start, end):
        self.history_range = (start, end)
        return [{
            "id": 1, "timestamp": start + 1, "type": "DevicePropertyUpdatedEvent",
            "data": {"id": 2145, "property": "heatRequired", "oldValue": False, "newValue": True},
        }]

    def get_device(self, device_id):
        return {
            "id": device_id,
            "name": f"Head {device_id}",
            "type": "com.fibaro.FGT001",
            "baseType": "com.fibaro.multilevelSwitch",
            "roomID": 8,
            "enabled": True,
            "visible": True,
            "properties": {
                "heatRequired": device_id in {2149, 2162, 2166, 2170, 2498, 2542},
                "parameters": json.dumps([{
                    "id": 3,
                    "value": (
                        5 if device_id in {2162, 2170, 2542}
                        else 4 if device_id in {2149, 2166, 2498}
                        else 1 if device_id in {2297, 2530, 2534, 2607}
                        else 0
                    ),
                }]),
                "heatingThermostatSetpoint": 21.0,
                "thermostatMode": "Heat",
                "windowOpened": False,
                "batteryLevel": 87,
                "wifiPassword": "must-not-leak",
            },
        }


class FibaroSnapshotTests(unittest.TestCase):
    def test_snapshot_returns_zone_and_head_snapshots_without_unapproved_fields(self):
        provider = FakeProvider()
        payload = build_hc3_snapshot(provider)
        self.assertEqual(payload["schema"], "fibaro-hc3-snapshot/1")
        self.assertEqual(payload["counts"], {"zones": 10, "heads": 15})
        self.assertEqual(len(payload["zones"]), 10)
        self.assertEqual(len(payload["heads"]), 15)
        self.assertEqual(payload["zones"][0]["properties"]["currentTemperature"], 20.5)
        self.assertEqual(payload["zones"][0]["properties"]["currentTemperatureHeating"], 21.5)
        self.assertNotIn("schedule", payload["zones"][0]["properties"])
        self.assertEqual({head["id"] for head in payload["heads"]}, set(provider.head_ids))
        self.assertNotIn("wifiPassword", payload["heads"][0]["properties"])
        kitchen_head = next(head for head in payload["heads"] if head["id"] == 2149)
        self.assertTrue(kitchen_head["properties"]["heatRequired"])
        self.assertEqual(kitchen_head["properties"]["parameter3"], 4)
        self.assertTrue(kitchen_head["properties"]["parameter3_heat_required"])
        self.assertEqual(kitchen_head["properties"]["heat_demand_consistency"], "consistent")


class FibaroApiHttpTests(unittest.TestCase):
    def setUp(self):
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), api.Handler)
        self.worker = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.worker.start()

    def tearDown(self):
        self.server.shutdown()
        self.worker.join(timeout=2)
        self.server.server_close()

    def request(self, path, token=None, method="GET"):
        headers = {"Authorization": f"Bearer {token}"} if token else {}
        with closing(http.client.HTTPConnection("127.0.0.1", self.server.server_port, timeout=3)) as client:
            client.request(method, path, headers=headers)
            response = client.getresponse()
            return response.status, json.loads(response.read() or b"{}")

    def test_snapshot_requires_separate_api_token(self):
        with patch.dict("os.environ", {"FIBARO_HC3_API_TOKEN": "test-api-token"}), \
             patch.object(api, "FibaroHC3Provider", return_value=FakeProvider()):
            status, payload = self.request("/api/fibaro/snapshot")
            self.assertEqual(status, 401)
            self.assertNotIn("heads", payload)

    def test_snapshot_is_available_with_valid_token(self):
        with patch.dict("os.environ", {
            "FIBARO_HC3_API_TOKEN": "test-api-token",
            "FIBARO_HC3_URL": "http://hc3.local",
            "FIBARO_HC3_USERNAME": "reader",
            "FIBARO_HC3_PASSWORD": "secret",
        }), patch.object(api, "FibaroHC3Provider", return_value=FakeProvider()):
            status, payload = self.request("/api/fibaro/snapshot", token="test-api-token")
        self.assertEqual(status, 200)
        self.assertEqual(payload["counts"], {"zones": 10, "heads": 15})

    def test_endpoint_fails_closed_when_api_token_is_missing(self):
        with patch.dict("os.environ", {}, clear=True):
            status, payload = self.request("/api/fibaro/health")
        self.assertEqual(status, 503)
        self.assertFalse(payload["configured"])

    def test_health_checks_hc3_connection(self):
        provider = FakeProvider()
        with patch.dict("os.environ", {
            "FIBARO_HC3_API_TOKEN": "test-api-token",
            "FIBARO_HC3_URL": "http://hc3.local",
            "FIBARO_HC3_USERNAME": "reader",
            "FIBARO_HC3_PASSWORD": "secret",
        }), patch.object(api, "FibaroHC3Provider", return_value=provider):
            status, payload = self.request("/api/fibaro/health", token="test-api-token")
        self.assertEqual(status, 200)
        self.assertTrue(payload["connected"])
        self.assertEqual(payload["zone_count"], 10)


    def test_event_history_endpoint_requires_bounded_range_and_returns_transitions(self):
        provider = FakeProvider()
        with patch.dict("os.environ", {
            "FIBARO_HC3_API_TOKEN": "test-api-token",
            "FIBARO_HC3_URL": "http://hc3.local",
            "FIBARO_HC3_USERNAME": "reader",
            "FIBARO_HC3_PASSWORD": "secret",
        }), patch.object(api, "FibaroHC3Provider", return_value=provider):
            missing_status, _ = self.request("/api/fibaro/history", token="test-api-token")
            status, payload = self.request(
                "/api/fibaro/history?start=100&end=200", token="test-api-token"
            )
            too_wide_status, _ = self.request(
                f"/api/fibaro/history?start=100&end={100 + 7 * 86400 + 1}",
                token="test-api-token",
            )
        self.assertEqual(missing_status, 400)
        self.assertEqual(status, 200)
        self.assertEqual(provider.history_range, (100, 200))
        self.assertEqual(payload["schema"], "thermoflow-fibaro-event-history/1")
        self.assertGreater(payload["counts"]["transitions"], 0)
        self.assertEqual(too_wide_status, 400)

    def test_mutating_methods_are_rejected(self):
        with patch.dict("os.environ", {"FIBARO_HC3_API_TOKEN": "test-api-token"}):
            for method in ("POST", "PUT", "PATCH", "DELETE"):
                status, _payload = self.request("/api/fibaro/snapshot", token="test-api-token", method=method)
                self.assertEqual(status, 405)


if __name__ == "__main__":
    unittest.main()
