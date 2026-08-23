"""Background pipeline runs with observable progress.

A pipeline run takes minutes, so it cannot block an HTTP request. One run is
allowed at a time; the UI polls `state()` for progress and the final summary.
"""

from __future__ import annotations

import threading
import traceback
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from ..pipeline import run_pipeline


@dataclass
class RunState:
    status: str = "idle"  # idle | running | done | error
    started_at: str | None = None
    finished_at: str | None = None
    run_id: str | None = None
    summary: dict[str, Any] | None = None
    error: str = ""
    log: list[str] = field(default_factory=list)


class PipelineRunner:
    """Serializes pipeline runs and exposes their state to the API."""

    def __init__(self, workspace_root: Path, config_dir: Path, data_dir: Path, output_dir: Path):
        self.workspace_root = workspace_root
        self.config_dir = config_dir
        self.data_dir = data_dir
        self.output_dir = output_dir
        self._lock = threading.Lock()
        self._thread: threading.Thread | None = None
        self._state = RunState()

    def state(self) -> dict[str, Any]:
        with self._lock:
            return asdict(self._state)

    @property
    def is_running(self) -> bool:
        with self._lock:
            return self._state.status == "running"

    def start(self) -> dict[str, Any]:
        with self._lock:
            if self._state.status == "running":
                return asdict(self._state)
            self._state = RunState(
                status="running",
                started_at=datetime.now(timezone.utc).isoformat(),
                log=["Starting pipeline run. This fetches every configured source and takes a few minutes."],
            )
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()
        return self.state()

    def _run(self) -> None:
        try:
            summary = run_pipeline(
                workspace_root=self.workspace_root,
                config_dir=self.config_dir,
                data_dir=self.data_dir,
                output_dir=self.output_dir,
            )
            payload = summary.model_dump(mode="json")
            with self._lock:
                self._state.status = "done"
                self._state.finished_at = datetime.now(timezone.utc).isoformat()
                self._state.run_id = summary.run_id
                self._state.summary = payload
                self._state.log.append(
                    f"Done. {summary.total_unique_jobs} unique jobs, "
                    f"{summary.accepted_jobs} accepted, {summary.rejected_jobs} rejected."
                )
        except Exception as exc:
            with self._lock:
                self._state.status = "error"
                self._state.finished_at = datetime.now(timezone.utc).isoformat()
                self._state.error = f"{type(exc).__name__}: {exc}"
                self._state.log.append(self._state.error)
            traceback.print_exc()
