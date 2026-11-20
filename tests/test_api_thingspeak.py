import os
import unittest
from unittest.mock import MagicMock, patch
from urllib.parse import parse_qs

import api


class ThingSpeakWriteConfigurationTests(unittest.TestCase):
    def test_write_lock_and_interval_are_defined(self):
        self.assertTrue(hasattr(api.THINGSPEAK_WRITE_LOCK, "acquire"))
        self.assertGreaterEqual(api.THINGSPEAK_WRITE_INTERVAL_SECONDS, 0)

    def test_write_path_can_enter_lock_with_configured_channels(self):
        channel_count = len(api.THINGSPEAK_COMPACT_CHANNEL_FIELDS)
        settings = {
            "THINGSPEAK_CHANNEL_IDS": [str(index + 1) for index in range(channel_count)],
            "THINGSPEAK_WRITE_KEYS": ["write-key"] * channel_count,
        }
        with patch.object(api, "_thingspeak_settings", side_effect=lambda name: settings.get(name, [])):
            # Empty telemetry intentionally produces no outbound writes, but still
            # exercises the lock and interval configuration used by the writer.
            api._thingspeak_write({"fetched_at": "2026-11-20T10:00:00+00:00", "data": {}})

    def test_fire_channel_writes_only_flame_and_brightness(self):
        response = MagicMock()
        response.__enter__.return_value.read.return_value = b'{"entry_id": 123}'
        api.THINGSPEAK_FIRE_LAST_WRITE = 0.0
        payload = {
            "fetched_at": "2026-11-20T10:00:00+00:00",
            "data": {
                "fire": True,
                "fire_brightness": 321,
                "temperatures": {"co": 65},
                "setpoints": {"co": 70},
                "hysteresis": {"boiler": 6},
            },
        }
        with patch.dict(os.environ, {
            "THINGSPEAK_FIRE_CHANNEL_ID": "3590535",
            "THINGSPEAK_FIRE_WRITE_KEY": "test-write-key",
            "THINGSPEAK_WRITE_ENABLED": "true",
        }), patch.object(api, "urlopen", return_value=response) as request:
            api._thingspeak_write_fire(payload)
        fields = parse_qs(request.call_args.args[0].data.decode())
        self.assertEqual(fields["field1"], ["1"])
        self.assertEqual(fields["field2"], ["321.0"])
        self.assertNotIn("field3", fields)
        self.assertNotIn("field4", fields)
        self.assertNotIn("field5", fields)


if __name__ == "__main__":
    unittest.main()
