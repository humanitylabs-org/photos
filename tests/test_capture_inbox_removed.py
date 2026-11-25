"""The capture inbox (box switch wizard/capture-inbox, humanitylabs-org/wizard-os#696) was dropped
(humanitylabs-org/wizard-os#949): its tools, the Agent Service probe and its stored batch state are gone.
Run: python3 -m unittest discover -s tests"""
import os
import unittest
from pathlib import Path

from test_front import load

ROOT = Path(__file__).resolve().parent.parent


class CaptureInboxRemoved(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.m = load()

    def test_tools_gone(self):
        m = self.m
        names = [t["name"] for t in m.rpc({"method": "tools/list", "id": 1})["result"]["tools"]]
        self.assertNotIn("photos_capture_batch", names)
        self.assertNotIn("photos_capture_file", names)
        r = m.rpc({"method": "tools/call", "id": 2, "params": {"name": "photos_capture_batch", "arguments": {"batch": "pb_1"}}})
        self.assertIn("error", r)
        self.assertFalse(hasattr(m, "CAPTURE_PATH") or hasattr(m, "CAPTURE_PERMISSIONS"))

    def test_no_capture_wiring_left(self):
        for rel in ("mcp/tools.py", "runtipi/docker-compose.yml", "SKILL.md", "README.md"):
            text = (ROOT / rel).read_text()
            self.assertNotIn("capture-inbox", text, rel)
            self.assertNotIn("PHOTOS_AGENT_INBOX_TOKEN", text, rel)

    def test_stored_batch_state_forgotten_at_start(self):
        m = self.m
        m._write_json("capture.json", {"active": True, "batches": {}})
        self.assertIn(m._drop_removed_switch_state, m.STARTUP)
        m._drop_removed_switch_state()
        self.assertFalse(os.path.exists(m._state_path("capture.json")))
        m._drop_removed_switch_state()  # idempotent


if __name__ == "__main__":
    unittest.main()
