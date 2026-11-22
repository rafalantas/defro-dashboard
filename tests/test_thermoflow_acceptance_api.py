import http.client
import json
import threading
import unittest
from contextlib import closing
from http.server import ThreadingHTTPServer
from unittest.mock import patch

import api
from thermoflow_production_acceptance import PATH, SCHEMA


class AcceptanceApiTests(unittest.TestCase):
    def test_acceptance_endpoint_is_read_only_and_does_not_fetch_upstream(self):
        with ThreadingHTTPServer(("127.0.0.1", 0), api.Handler) as server:
            worker = threading.Thread(target=server.serve_forever, daemon=True)
            worker.start()
            try:
                with (patch.object(api, "_shadow_runtime") as shadow,
                      patch.object(api, "_live_ml_runtime") as live,
                      patch.object(api, "_fuel_summary_for_intelligence",
                                   side_effect=AssertionError("unexpected upstream call"))):
                    shadow.read.return_value = {"status":"collecting"}
                    live.fuel_path = None
                    live.read.return_value = {
                        "burner_ml":{"status":"collecting"},
                        "fuel_ml":{"status":"collecting"}}
                    with closing(http.client.HTTPConnection("127.0.0.1", server.server_port, timeout=3)) as client:
                        client.request("GET", PATH)
                        response = client.getresponse()
                        payload = json.loads(response.read())
                        self.assertEqual(response.status, 200)
                        self.assertEqual(payload["schema"], SCHEMA)
                        self.assertEqual(payload["status"], "incomplete")
                        self.assertFalse(payload["automatic_signoff"])
                        for method in ("POST", "PUT", "PATCH", "DELETE"):
                            client.request(method, PATH)
                            self.assertIn(client.getresponse().status, (404, 405, 501))
                    shadow.read.assert_called_once()
                    live.read.assert_called_once()
            finally:
                server.shutdown()
                worker.join(timeout=2)


if __name__ == "__main__":
    unittest.main()
