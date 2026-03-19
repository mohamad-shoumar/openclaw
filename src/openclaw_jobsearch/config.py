from __future__ import annotations

import json
from pathlib import Path

from .models import ProfileConfig, RulesConfig, WatchlistConfig


def _read_json(path: Path) -> dict:
    return json.loads(path.read_text())


class AppConfig:
    def __init__(self, workspace_root: Path, config_dir: Path):
        self.workspace_root = workspace_root
        self.config_dir = config_dir
        self.profile = ProfileConfig.model_validate(_read_json(config_dir / "profile.json"))
        self.rules = RulesConfig.model_validate(_read_json(config_dir / "rules.json"))
        self.watchlist = WatchlistConfig.model_validate(_read_json(config_dir / "watchlist.json"))

    @property
    def resume_path(self) -> Path:
        return self.workspace_root / self.profile.resume_path

    @property
    def resume_text_path(self) -> Path:
        return self.workspace_root / self.profile.resume_text_path

    def ensure_inputs_exist(self) -> None:
        missing = []
        if not self.resume_path.exists():
            missing.append(str(self.resume_path))
        if not self.resume_text_path.exists():
            missing.append(str(self.resume_text_path))
        if missing:
            joined = ", ".join(missing)
            raise FileNotFoundError(f"Required input files are missing: {joined}")
