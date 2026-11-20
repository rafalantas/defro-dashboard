import unittest
from unittest.mock import patch

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


if __name__ == "__main__":
    unittest.main()
