"""Optional third-party history sources. Disabled unless configured AND keyed.

API keys are read only from environment variables, never from config.json.
Plugins run only to backfill a stream whose official history is truncated,
and their rows are stored with source ``plugin:<name>`` (lowest merge rank),
so official API/S3 copies of the same record always win.
"""
from __future__ import annotations

import os


class SourcePlugin:
    name = ""
    env_key = ""
    capabilities = frozenset()
    cost_hint = ""

    def __init__(self, settings=None):
        self.settings = dict(settings or {})

    def available(self):
        return bool(os.environ.get(self.env_key))

    def test(self, wallet):
        """One minimal authenticated request; return {"ok": bool, "detail": str}."""
        raise NotImplementedError

    def fetch(self, store, kind, start_ms, end_ms, *, log=print, cancel_check=None):
        """Store rows of ``kind`` in [start_ms, end_ms]; return a stats dict."""
        raise NotImplementedError
