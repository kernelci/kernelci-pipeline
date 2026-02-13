# SPDX-License-Identifier: LGPL-2.1-or-later
#
# Copyright (C) 2026 Collabora Limited
# Author: Denys Fedoryshchenko <denys.f@collabora.com>

"""Telemetry emitter for pipeline services.

Buffers telemetry events and periodically flushes them to the
KernelCI API. Falls back to local JSONL file when API is unreachable.
"""

import json
import logging
import os
import threading
import time
from datetime import datetime, timezone


logger = logging.getLogger(__name__)

# Default fallback JSONL path when API flush fails
# May be need to point some persistent volume mount?
DEFAULT_FALLBACK_PATH = '/tmp/kci-telemetry-fallback.jsonl'


class TelemetryEmitter:
    """Buffered telemetry event emitter.

    Accumulates events in memory and flushes them to the API in
    batches. A background daemon thread handles periodic flushes.

    Args:
        api: KernelCI API object (must have .telemetry.add() method)
        service_name: Name of the emitting service (for logging)
        buffer_size: Max events before auto-flush (default 50)
        flush_interval: Seconds between periodic flushes (default 30)
        fallback_path: JSONL file path for API failure fallback
    """

    def __init__(self, api, service_name,
                 buffer_size=50, flush_interval=30,
                 fallback_path=None):
        self._api = api
        self._service_name = service_name
        self._buffer_size = buffer_size
        self._flush_interval = flush_interval
        self._fallback_path = fallback_path or DEFAULT_FALLBACK_PATH
        self._buffer = []
        self._lock = threading.Lock()
        self._closed = False

        # Start background flush thread
        self._flush_thread = threading.Thread(
            target=self._periodic_flush,
            name=f'telemetry-{service_name}',
            daemon=True,
        )
        self._flush_thread.start()

    def emit(self, kind, **kwargs):
        """Add a telemetry event to the buffer.

        Args:
            kind: Event kind (runtime_error, job_submission,
                  job_skip, job_result, test_result)
            **kwargs: Event fields (runtime, device_type, etc.)
        """
        if self._closed:
            return

        event = {
            'kind': kind,
            'ts': datetime.now(timezone.utc).isoformat(),
        }
        event.update(kwargs)

        with self._lock:
            self._buffer.append(event)
            if len(self._buffer) >= self._buffer_size:
                self._flush_locked()

    def close(self):
        """Final flush and stop the emitter."""
        self._closed = True
        with self._lock:
            self._flush_locked()

    def _periodic_flush(self):
        """Background thread: flush buffer at regular intervals."""
        while not self._closed:
            time.sleep(self._flush_interval)
            with self._lock:
                if self._buffer:
                    self._flush_locked()

    def _flush_locked(self):
        """Flush buffer to API. Must be called with self._lock held."""
        if not self._buffer:
            return

        events = self._buffer.copy()
        self._buffer.clear()

        try:
            self._api.telemetry.add(events)
        except Exception as exc:
            logger.warning(
                "Telemetry API flush failed (%s), "
                "writing %d events to %s: %s",
                self._service_name, len(events),
                self._fallback_path, exc,
            )
            self._write_fallback(events)

    def _write_fallback(self, events):
        """Append events to local JSONL file as fallback."""
        try:
            fallback_dir = os.path.dirname(self._fallback_path)
            if fallback_dir and not os.path.exists(fallback_dir):
                os.makedirs(fallback_dir, exist_ok=True)
            with open(self._fallback_path, 'a') as f:
                for event in events:
                    f.write(json.dumps(event) + '\n')
        except Exception as exc:
            logger.error(
                "Telemetry fallback write failed: %s", exc
            )
