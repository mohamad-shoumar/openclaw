from __future__ import annotations

import json
from pathlib import Path

from .feedback import FeedbackStore
from .models import PathsConfig, ProfileConfig, RulesConfig, WatchlistConfig
from .registry import load_worldwide_company_registry, normalize_company_name


def _read_json(path: Path) -> dict:
    return json.loads(path.read_text())


def _load_paths(config_dir: Path) -> PathsConfig:
    """paths.json is optional; every field has a working default."""
    path = config_dir / "paths.json"
    if not path.exists():
        return PathsConfig()
    return PathsConfig.model_validate(_read_json(path))


class AppConfig:
    def __init__(self, workspace_root: Path, config_dir: Path):
        self.workspace_root = workspace_root
        self.config_dir = config_dir
        self.paths = _load_paths(config_dir)
        self.profile = ProfileConfig.model_validate(_read_json(config_dir / "profile.json"))
        self.rules = RulesConfig.model_validate(_read_json(config_dir / "rules.json"))
        self.watchlist = WatchlistConfig.model_validate(_read_json(config_dir / "watchlist.json"))
        self.worldwide_registry_path = workspace_root / self.paths.worldwide_registry
        self.worldwide_companies = load_worldwide_company_registry(self.worldwide_registry_path)
        self.worldwide_company_index = {
            company.company_key: company for company in self.worldwide_companies
        }
        self.feedback = FeedbackStore.load(config_dir)

    @property
    def resume_path(self) -> Path:
        return self.workspace_root / self.paths.resume_pdf

    @property
    def resume_text_path(self) -> Path:
        return self.workspace_root / self.paths.resume_text

    @property
    def resume_guide_path(self) -> Path:
        return self.workspace_root / self.paths.resume_guide

    @property
    def cover_letter_guide_path(self) -> Path:
        return self.workspace_root / self.paths.cover_letter_guide

    def ensure_inputs_exist(self) -> None:
        missing = []
        if not self.resume_path.exists():
            missing.append(str(self.resume_path))
        if not self.resume_text_path.exists():
            missing.append(str(self.resume_text_path))
        if missing:
            joined = ", ".join(missing)
            raise FileNotFoundError(f"Required input files are missing: {joined}")

    def is_worldwide_company(self, company_name: str) -> bool:
        return normalize_company_name(company_name) in self.worldwide_company_index
