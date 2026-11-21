import json
import threading
import unittest
from datetime import datetime, timezone
from http.server import ThreadingHTTPServer
from unittest.mock import patch
from urllib.error import HTTPError
from urllib.request import Request, urlopen

import api
from thermoflow_shadow_runtime import PATH, SCHEMA, ShadowRuntime


def payload(timestamp, *, fire=1, temperature=50):
    return {"connected": True, "fetched_at": datetime.fromtimestamp(timestamp, timezone.utc).isoformat(),
            "data": {"fire": fire, "pumps": {"floor": True, "valve4d": True, "dhw": False},
                     "temperatures": {"flue": 90}, "valve": {"return": temperature}}}


class ShadowRuntimeTests(unittest.TestCase):
    def test_warms_with_new_samples_and_expires_without_new_reads(self):
        runtime = ShadowRuntime()
        for i in range(12):
            data = payload(1000 + i * 30)
            runtime.observe(data)
            runtime.observe(data)
        self.assertEqual(runtime.read(1330)["status"], "collecting")
        runtime.observe(payload(1360, temperature=56))
        report = runtime.read(1360)
        self.assertEqual(report["status"], "screened")
        self.assertEqual(report["health"]["score"], 50)
        self.assertEqual(report["diagnostics"]["items"][0]["component"], "return")
        self.assertNotIn("health", runtime.read(1451))
        self.assertEqual(runtime.read(1451)["status"], "stale")
        runtime.observe(payload(1390, fire=0))
        self.assertEqual(runtime.read(1390)["status"], "collecting")
        runtime.observe({"connected": False})
        self.assertEqual(runtime.read(1390)["status"], "telemetry_unavailable")

    def test_route_only_reads_ram_and_disallows_post(self):
        with ThreadingHTTPServer(("127.0.0.1", 0), api.Handler) as server:
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            try:
                url = f"http://127.0.0.1:{server.server_port}{PATH}"
                with patch.object(api, "_shadow_runtime", ShadowRuntime()), \
                        patch.object(api, "snapshot", side_effect=AssertionError("unexpected upstream")) as upstream:
                    with urlopen(url, timeout=3) as response:
                        self.assertEqual(response.headers["Cache-Control"], "no-store")
                        self.assertEqual(json.load(response)["schema"], SCHEMA)
                    with self.assertRaises(HTTPError) as caught:
                        urlopen(Request(url, method="POST"), timeout=3)
                    self.assertEqual(caught.exception.code, 404)
                    caught.exception.close()
                    upstream.assert_not_called()
            finally:
                server.shutdown()
                thread.join(timeout=2)
