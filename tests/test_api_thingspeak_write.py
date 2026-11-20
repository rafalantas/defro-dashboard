"""Regression coverage for the dashboard's compact ThingSpeak writer."""
import os
import unittest
from unittest.mock import Mock, patch

import api


class ThingSpeakWriteTests(unittest.TestCase):
    def test_interval_uses_default_minimum_and_invalid_value_fallback(self):
        with patch.dict(os.environ, {}, clear=True):
            self.assertEqual(api._thingspeak_write_interval_seconds(), 40)
        with patch.dict(os.environ, {"THINGSPEAK_WRITE_INTERVAL_SECONDS": "20"}, clear=True):
            self.assertEqual(api._thingspeak_write_interval_seconds(), 35)
        with patch.dict(os.environ, {"THINGSPEAK_WRITE_INTERVAL_SECONDS": "75"}, clear=True):
            self.assertEqual(api._thingspeak_write_interval_seconds(), 75)
        with patch.dict(os.environ, {"THINGSPEAK_WRITE_INTERVAL_SECONDS": "invalid"}, clear=True):
            self.assertEqual(api._thingspeak_write_interval_seconds(), 40)

    def test_writer_serializes_and_throttles_compact_channel_writes(self):
        response = Mock()
        response.__enter__ = Mock(return_value=response)
        response.__exit__ = Mock(return_value=False)
        response.read.return_value = b'{"entry_id":123}'
        last_writes = [0.0, 0.0]

        with (
            patch.dict(os.environ, {
                "THINGSPEAK_WRITE_ENABLED": "true",
                "THINGSPEAK_CHANNEL_IDS": "1,2",
                "THINGSPEAK_WRITE_KEYS": "key-1,key-2",
            }, clear=True),
            patch.object(api, "THINGSPEAK_LAST_WRITES", last_writes),
            patch.object(api.time, "monotonic", return_value=1000.0),
            patch.object(api, "urlopen", return_value=response) as urlopen,
        ):
            payload = {
                "fetched_at": "2026-11-20T10:00:00+01:00",
                "data": {"temperatures": {"co": 65.0}},
            }
            api._thingspeak_write(payload)
            api._thingspeak_write(payload)

        self.assertEqual(urlopen.call_count, 1)
        request = urlopen.call_args.args[0]
        self.assertIn(b"field1=65.0", request.data)
        self.assertEqual(last_writes, [1000.0, 0.0])


if __name__ == "__main__":
    unittest.main()
