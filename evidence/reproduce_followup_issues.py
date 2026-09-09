"""Host-only regression checks for the previously reproduced defects.

Run from the repository root:
  .venv/bin/python evidence/reproduce_followup_issues.py
Original failure evidence remains in 20260907_followup_host_reproduction.json.
"""
from pathlib import Path
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

if __name__ == "__main__":
    names = [
        "test_sdo_batched_unrelated_response",
        "test_periodic_holds_gate_until_cleanup_finishes",
        "test_periodic_startup_failure_always_stops_and_closes",
        "test_periodic_startup_cleanup_error_reaches_session_result",
    ]
    suite = unittest.defaultTestLoader.loadTestsFromNames([
        "test_jcan_mcp.McpHostTest." + name for name in names
    ])
    result = unittest.TextTestRunner(verbosity=2).run(suite)
    sys.exit(not result.wasSuccessful())
