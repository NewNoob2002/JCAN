# Legacy Python/MCP implementation

This directory preserves the pre-1.0 Python CLI, MCP server, tests, package metadata, bus profile, Agent integration plan, and upper-computer design review.

It is retained for historical investigation only. The supported control surface is the Rust CLI in the repository root. Do not run these files for JTool-CAN hardware operations.

The last archival host-test run on 2026-09-09 completed 16 tests, skipped 5 hardware-gated tests, and reported 3 errors in deprecated timeout/stdio lifecycle tests. Those failures are not release gates for Rust CLI 1.0.
