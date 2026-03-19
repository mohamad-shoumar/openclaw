from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from typing import Iterable
from urllib.parse import urlencode
from urllib.request import Request, urlopen

from .models import RawJob, WatchlistConfig


DEFAULT_HEADERS = {
    "User-Agent": "openclaw-jobsearch/0.1 (+https://openclaw.local)"
}


def fetch_json(url: str) -> dict | list:
    request = Request(url, headers=DEFAULT_HEADERS)
    with urlopen(request, timeout=30) as response:
        return json.loads(response.read().decode("utf-8"))


def fetch_json_plain(url: str) -> dict | list:
    with urlopen(url, timeout=30) as response:
        return json.loads(response.read().decode("utf-8"))


class SourceAdapter:
    name = "source"

    def fetch(self) -> list[RawJob]:
        raise NotImplementedError


class GreenhouseSource(SourceAdapter):
    name = "greenhouse"

    def __init__(self, boards: Iterable):
        self.boards = list(boards)

    def fetch(self) -> list[RawJob]:
        jobs: list[RawJob] = []
        for board in self.boards:
            url = f"https://boards-api.greenhouse.io/v1/boards/{board.board_token}/jobs?content=true"
            try:
                payload = fetch_json(url)
            except Exception:
                continue
            for item in payload.get("jobs", []):
                external_id = str(item.get("id", ""))
                jobs.append(
                    RawJob(
                        discovery_source=self.name,
                        source_tier="ats",
                        board_type="greenhouse",
                        external_id=f"{board.board_token}:{external_id}",
                        fetched_at=datetime.now(timezone.utc),
                        payload={
                            "board_company": board.company,
                            "board_token": board.board_token,
                            "job": item,
                        },
                    )
                )
        return jobs


class LeverSource(SourceAdapter):
    name = "lever"

    def __init__(self, boards: Iterable):
        self.boards = list(boards)

    def fetch(self) -> list[RawJob]:
        jobs: list[RawJob] = []
        for board in self.boards:
            url = f"https://api.lever.co/v0/postings/{board.board_token}?mode=json"
            try:
                payload = fetch_json(url)
            except Exception:
                continue
            for item in payload:
                external_id = str(item.get("id", ""))
                jobs.append(
                    RawJob(
                        discovery_source=self.name,
                        source_tier="ats",
                        board_type="lever",
                        external_id=f"{board.board_token}:{external_id}",
                        fetched_at=datetime.now(timezone.utc),
                        payload={
                            "board_company": board.company,
                            "board_token": board.board_token,
                            "job": item,
                        },
                    )
                )
        return jobs


class AshbySource(SourceAdapter):
    name = "ashby"

    def __init__(self, boards: Iterable):
        self.boards = list(boards)

    def fetch(self) -> list[RawJob]:
        jobs: list[RawJob] = []
        for board in self.boards:
            params = urlencode({"includeCompensation": "true"})
            url = f"https://api.ashbyhq.com/posting-api/job-board/{board.board_token}?{params}"
            try:
                payload = fetch_json(url)
            except Exception:
                continue
            for item in payload.get("jobs", []):
                external_id = str(item.get("id", ""))
                jobs.append(
                    RawJob(
                        discovery_source=self.name,
                        source_tier="ats",
                        board_type="ashby",
                        external_id=f"{board.board_token}:{external_id}",
                        fetched_at=datetime.now(timezone.utc),
                        payload={
                            "board_company": board.company,
                            "board_token": board.board_token,
                            "job": item,
                        },
                    )
                )
        return jobs


class SerpApiSource(SourceAdapter):
    name = "serpapi"

    def __init__(self, serpapi_config):
        self.config = serpapi_config
        self.api_key = os.getenv("SERPAPI_API_KEY", "")

    def fetch(self) -> list[RawJob]:
        if not self.config or not self.api_key:
            return []
        jobs: list[RawJob] = []
        for query in self.config.queries:
            next_page_token: str | None = None
            for page in range(self.config.pages_per_query):
                query_params = {
                    "engine": self.config.engine,
                    "q": query,
                    "api_key": self.api_key,
                }
                if next_page_token:
                    query_params["next_page_token"] = next_page_token
                params = urlencode(query_params)
                url = f"https://serpapi.com/search.json?{params}"
                try:
                    payload = fetch_json_plain(url)
                except Exception:
                    continue
                for item in payload.get("jobs_results", []):
                    external_id = str(item.get("job_id", item.get("id", "")))
                    jobs.append(
                        RawJob(
                            discovery_source=self.name,
                            source_tier="aggregator",
                            board_type="serpapi",
                            external_id=f"{query}:{external_id}",
                            fetched_at=datetime.now(timezone.utc),
                            payload={
                                "query": query,
                                "job": item,
                            },
                        )
                    )
                next_page_token = payload.get("serpapi_pagination", {}).get("next_page_token")
                if not next_page_token:
                    break
        return jobs


class RemotiveSource(SourceAdapter):
    name = "remotive"

    def __init__(self, remote_api_config):
        self.config = remote_api_config

    def fetch(self) -> list[RawJob]:
        if not self.config:
            return []
        try:
            payload = fetch_json("https://remotive.com/api/remote-jobs")
        except Exception:
            return []
        jobs: list[RawJob] = []
        for item in payload.get("jobs", [])[: self.config.limit]:
            external_id = str(item.get("id", ""))
            jobs.append(
                RawJob(
                    discovery_source=self.name,
                    source_tier="remote_api",
                    board_type="remotive",
                    external_id=external_id,
                    fetched_at=datetime.now(timezone.utc),
                    payload={"job": item},
                )
            )
        return jobs


def build_sources(watchlist: WatchlistConfig) -> list[SourceAdapter]:
    return [
        GreenhouseSource(watchlist.greenhouse_boards),
        LeverSource(watchlist.lever_boards),
        AshbySource(watchlist.ashby_boards),
        SerpApiSource(watchlist.serpapi),
        RemotiveSource(watchlist.remote_api),
    ]
