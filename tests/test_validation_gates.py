"""Gate-level tests for `validate_job`.

Each test pins one gate against the adopted `config/rules.json`, because the
gates are keyword-driven and a rules edit is the easiest way to break one
silently. `validate_job` collects every failure reason rather than stopping at
the first, so a test asserts on the presence of its own reason, not on the
count.
"""

from __future__ import annotations

from datetime import date
from pathlib import Path

import pytest

from openclaw_jobsearch.config import AppConfig
from openclaw_jobsearch.models import JobRecord
from openclaw_jobsearch.pipeline import _extract_experience, validate_job

WORKSPACE_ROOT = Path(__file__).resolve().parent.parent
AS_OF = date(2026, 8, 31)


@pytest.fixture(scope="module")
def config() -> AppConfig:
    return AppConfig(WORKSPACE_ROOT, WORKSPACE_ROOT / "config")


def make_job(title: str, description: str, location: str, workplace_type: str = "remote") -> JobRecord:
    return JobRecord(
        job_slug="test-job",
        run_id="test-run",
        discovery_source="greenhouse",
        source_tier="ats",
        board_type="greenhouse",
        external_id="1",
        company="Test Company",
        title=title,
        job_url="https://example.com/job",
        apply_url="https://example.com/apply",
        description_text=description,
        location_raw=location,
        workplace_type=workplace_type,
    )


def score(config: AppConfig, **kwargs) -> JobRecord:
    return validate_job(make_job(**kwargs), config, as_of=AS_OF)


def test_python_ecosystem_evidence_satisfies_the_required_skill_gate(config):
    """A posting can be a Python job without the word "python" surviving the scrape."""
    job = score(
        config,
        title="Backend Engineer",
        description="You will build services with FastAPI and Celery.",
        location="Remote - Worldwide",
    )
    assert job.validation_status == "accepted", job.rejection_reasons


def test_required_skill_gate_still_rejects_a_non_python_stack(config):
    job = score(
        config,
        title="Ruby Engineer",
        description="Rails and Sidekiq. Two years of experience.",
        location="Remote - Worldwide",
    )
    assert "Python is not clearly part of the role requirements." in job.rejection_reasons


def test_non_posting_titles_are_rejected(config):
    """The talent-pool and policy-page titles the target-role gate used to catch."""
    for title in ["Engineer (Talent Pool)", "Company Code of Conduct", "Open Application"]:
        job = score(
            config,
            title=title,
            description="Python and FastAPI.",
            location="Remote - Worldwide",
        )
        assert "Title is not a job posting." in job.rejection_reasons, title


def test_us_remote_in_the_location_is_a_restriction(config):
    job = score(
        config,
        title="Backend Engineer",
        description="Python and FastAPI.",
        location="New York or US Remote",
    )
    assert job.remote_scope == "restricted"
    assert "Remote scope 'restricted' is not in the allowed scopes." in job.rejection_reasons


def test_unknown_remote_scope_reaches_review(config):
    """Absent remote evidence is not negative evidence, so it is queued, not dropped."""
    job = score(
        config,
        title="Backend Engineer",
        description="Python and FastAPI.",
        location="",
        workplace_type="",
    )
    assert job.remote_scope == "unknown"
    assert job.validation_status == "accepted", job.rejection_reasons


def test_excluded_function_is_rejected_by_title(config):
    job = score(
        config,
        title="Sales Engineer",
        description="Python and FastAPI.",
        location="Remote - Worldwide",
    )
    assert "Role function is on the excluded list." in job.rejection_reasons


def test_unrecognised_but_plausible_title_is_no_longer_dropped(config):
    """With `require_target_role_match` off, an odd title goes to manual review."""
    assert config.rules.require_target_role_match is False
    job = score(
        config,
        title="Pythonista Wrangler",
        description="Python and Django services.",
        location="Remote - Worldwide",
    )
    assert job.validation_status == "accepted", job.rejection_reasons


# --- years-of-experience gate -------------------------------------------------
# The old extractor flattened "5+ years" into a closed (5, 5) range and the gate
# compared only that range ceiling with a strict `>`, so a 5+ posting sailed past
# a maximum of 5. These pin both halves of that failure.


@pytest.mark.parametrize(
    ("description", "expected"),
    [
        ("5+ years of software engineering experience.", (5, None, True)),
        ("Minimum of 3+ years of hands-on experience.", (3, None, True)),
        ("3+ yrs managing production Linux.", (3, None, True)),
        ("We want at least five years of experience.", (5, None, True)),
        ("3 - 5 years of experience.", (3, 5, False)),
        ("3 to 5 years of experience.", (3, 5, False)),
        ("2 years of experience required.", (2, 2, False)),
        ("5 yrs. of experience.", (5, 5, False)),
        ("No number stated anywhere.", (None, None, False)),
    ],
)
def test_experience_extraction_keeps_the_open_ended_marker(description, expected):
    minimum, maximum, open_ended, _ = _extract_experience(description)
    assert (minimum, maximum, open_ended) == expected


def test_experience_floor_comes_from_the_requirements_block():
    """A nice-to-have count must not stand in for the real requirement."""
    description = "Nice to have: 2+ years of Python. Requirements: 8+ years of backend experience."
    minimum, maximum, open_ended, _ = _extract_experience(description)
    assert (minimum, maximum, open_ended) == (8, None, True)


def test_experience_ignores_elapsed_time_counts():
    """"Founded 3 years ago" is company history, not a requirement."""
    description = "Founded 3 years ago. Requirements: 4+ years of Python."
    minimum, _, open_ended, _ = _extract_experience(description)
    assert (minimum, open_ended) == (4, True)


def test_open_ended_requirement_on_the_ceiling_is_rejected(config):
    """The exact regression: 5+ years against a maximum of 5."""
    assert config.rules.max_required_experience_years == 5
    job = score(
        config,
        title="Software Engineer, Mobile",
        description="Requirements: 5+ years of software engineering experience with Python.",
        location="Remote - Worldwide",
    )
    assert job.experience_open_ended is True
    assert job.experience_required_max is None
    assert "Requires 5+ years, above the strict maximum of 5." in job.rejection_reasons


def test_closed_range_inside_the_ceiling_is_accepted(config):
    """"3 - 5 years" fits the tolerance, so the fix must not over-reject."""
    job = score(
        config,
        title="Backend Engineer",
        description="Requirements: 3 - 5 years of experience with Python and FastAPI.",
        location="Remote - Worldwide",
    )
    assert job.validation_status == "accepted", job.rejection_reasons


def test_open_ended_requirement_below_the_ceiling_is_accepted(config):
    job = score(
        config,
        title="Backend Engineer",
        description="Requirements: 3+ years of experience with Python and FastAPI.",
        location="Remote - Worldwide",
    )
    assert job.validation_status == "accepted", job.rejection_reasons


@pytest.mark.parametrize(
    "description",
    [
        # "in business" as a decoy cost more real requirements than it caught.
        "Here's what we're looking for: 8+ years in business systems analysis.",
        # Scraped postings carry mojibake where an apostrophe was; `\w` counted
        # those bytes as letters and the unit-word boundary swallowed the match.
        "Qualifications: â 8+ yearsâ experience as a software engineer",
    ],
)
def test_experience_survives_trailing_context_and_mojibake(description):
    minimum, _, open_ended, _ = _extract_experience(description)
    assert (minimum, open_ended) == (8, True)


@pytest.mark.parametrize(
    "description",
    [
        "For the last 5 years I have been living in a small beach town.",
        "Over the next 10 years, every company will need to embrace AI.",
        "8 years ago, Jeremy was frustrated with the tooling.",
        "We set out more than 160 years ago to promote security of life at sea.",
    ],
)
def test_elapsed_time_counts_are_not_requirements(description):
    assert _extract_experience(description)[0] is None


def test_for_over_n_years_is_company_history_not_a_requirement():
    """"for over 4 years" is elapsed time; a bare "over 4 years" is the bar."""
    assert _extract_experience("We have been licensing Kraken for over 4 years.")[0] is None
    assert _extract_experience("You have over 4 years of Python experience.")[:3] == (4, None, True)
