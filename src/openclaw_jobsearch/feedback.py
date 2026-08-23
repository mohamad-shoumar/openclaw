"""Structured reviewer feedback that generalizes to future runs.

A verdict on a single job is close to worthless: rejecting one FlexBoard posting
does nothing about the next eight. Feedback is therefore recorded against a
reusable *dimension* of the job (the apply domain, the company, a title phrase,
the discovery source), so one decision keeps applying on every later run.

Rules live in `config/feedback.json`, are applied during validation, and can be
retro-applied to jobs already sitting in the review queue.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Iterable
from urllib.parse import urlsplit

FEEDBACK_FILENAME = "feedback.json"

# kind -> human description, shown by `feedback list` and in --help.
RULE_KINDS = {
    "apply_domain": "Host of the apply/job URL. Suffix match, so 'up.railway.app' covers every subdomain.",
    "company": "Company name. Case-insensitive exact match after normalization.",
    "title_pattern": "Phrase appearing in the job title. Case-insensitive substring.",
    "source": "Discovery source, e.g. 'serpapi' or 'generic_careers'.",
    "required_domain": (
        "Comma-separated business-domain terms, e.g. 'healthcare, ehr, hipaa'. Blocks only when "
        "the posting demands the domain as a hard requirement; bonus/preferred mentions pass."
    ),
    "work_authorization": (
        "Comma-separated region terms, e.g. 'u.s, usa, united states, eu, canada'. Blocks postings "
        "that demand work authorization/residency in one of them or refuse visa sponsorship."
    ),
    "job": "A single job slug. Does not generalize; use only for genuine one-offs.",
}


@dataclass
class FeedbackRule:
    id: str
    kind: str
    value: str
    reason: str
    created_at: str
    origin_job: str = ""

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "kind": self.kind,
            "value": self.value,
            "reason": self.reason,
            "created_at": self.created_at,
            "origin_job": self.origin_job,
        }


class FeedbackStore:
    def __init__(self, rules: list[FeedbackRule], path: Path | None = None):
        self.rules = rules
        self.path = path

    @classmethod
    def load(cls, config_dir: Path) -> "FeedbackStore":
        path = config_dir / FEEDBACK_FILENAME
        if not path.exists():
            return cls([], path)
        payload = json.loads(path.read_text())
        rules = [FeedbackRule(**entry) for entry in payload.get("rules", [])]
        return cls(rules, path)

    def save(self) -> None:
        if self.path is None:
            raise ValueError("FeedbackStore has no path to save to.")
        body = {
            "_comment": "Reviewer feedback. Each rule blocks a whole class of jobs, not one posting.",
            "rules": [rule.to_dict() for rule in self.rules],
        }
        self.path.write_text(json.dumps(body, indent=2) + "\n")

    def next_id(self, kind: str) -> str:
        existing = sum(1 for rule in self.rules if rule.kind == kind)
        return f"{kind}-{existing + 1:03d}"

    def add(self, kind: str, value: str, reason: str, origin_job: str = "") -> FeedbackRule:
        if kind not in RULE_KINDS:
            raise ValueError(f"Unknown feedback kind '{kind}'. Valid: {', '.join(RULE_KINDS)}")
        normalized = _normalize_value(kind, value)
        if not normalized:
            raise ValueError(f"Empty value for feedback kind '{kind}'.")
        for rule in self.rules:
            if rule.kind == kind and rule.value == normalized:
                return rule
        rule = FeedbackRule(
            id=self.next_id(kind),
            kind=kind,
            value=normalized,
            reason=reason,
            created_at=date.today().isoformat(),
            origin_job=origin_job,
        )
        self.rules.append(rule)
        return rule

    def remove(self, rule_id: str) -> bool:
        before = len(self.rules)
        self.rules = [rule for rule in self.rules if rule.id != rule_id]
        return len(self.rules) < before

    def match(self, job) -> FeedbackRule | None:
        """Return the first rule that blocks this job, if any."""
        host = _apply_host(job)
        company = _normalize_value("company", job.company)
        title = (job.title or "").lower()
        for rule in self.rules:
            if rule.kind == "apply_domain" and host and _host_matches(host, rule.value):
                return rule
            if rule.kind == "company" and company and company == rule.value:
                return rule
            if rule.kind == "title_pattern" and rule.value in title:
                return rule
            if rule.kind == "source" and job.discovery_source == rule.value:
                return rule
            if rule.kind == "required_domain" and domain_required(
                job.title, getattr(job, "description_text", ""), rule.value
            ):
                return rule
            if rule.kind == "work_authorization" and work_auth_required(
                job.title, getattr(job, "description_text", ""), rule.value
            ):
                return rule
            if rule.kind == "job" and job.job_slug == rule.value:
                return rule
        return None


def _normalize_value(kind: str, value: str) -> str:
    value = (value or "").strip()
    if kind == "apply_domain":
        return _host_from_url(value) or value.lower().lstrip(".")
    if kind == "company":
        return " ".join(value.lower().split())
    if kind in {"title_pattern", "source"}:
        return value.lower()
    if kind in {"required_domain", "work_authorization"}:
        terms: list[str] = []
        for term in value.lower().split(","):
            term = " ".join(term.split())
            if term and term not in terms:
                terms.append(term)
        return ", ".join(terms)
    return value


def _host_from_url(value: str) -> str:
    if "://" not in value:
        return ""
    host = urlsplit(value).netloc.lower()
    return host[4:] if host.startswith("www.") else host


def _apply_host(job) -> str:
    for candidate in (job.apply_url, job.job_url):
        host = _host_from_url(candidate or "")
        if host:
            return host
    return ""


def _host_matches(host: str, blocked: str) -> bool:
    """Suffix match so a rule on 'up.railway.app' covers every subdomain of it."""
    return host == blocked or host.endswith("." + blocked)


# --- required_domain matching -------------------------------------------------
# The whole point of this kind is telling "healthcare experience is a MUST" apart
# from "healthcare experience is a plus". The description is scanned per sentence:
# a section header ("Requirements" vs "Nice to have") sets the default reading and
# explicit cues on the sentence itself override it. Softener cues win over hard
# cues because "experience with EHR is a plus" contains both. Mentions with no
# signal at all (e.g. the company blurb) never block.

_PREFERRED_HEADERS = (
    "nice to have",
    "nice-to-have",
    "bonus",
    "preferred",
    "good to have",
    "great to have",
    "not required",
)
_REQUIRED_HEADERS = (
    "requirement",
    "qualification",
    "must have",
    "must-have",
    "what you need",
    "what you'll need",
    "what we're looking for",
    "who you are",
    "you have",
    "essential",
    "minimum",
)
_PREFERRED_CUES = (
    "nice to have",
    "nice-to-have",
    "bonus",
    "a plus",
    "big plus",
    "huge plus",
    "plus point",
    "preferred",
    "not required",
    "not a must",
    "not mandatory",
    "familiarity with",
    "would be great",
    "advantageous",
    "good to have",
    "great to have",
    "helpful",
    "optional",
)
_REQUIRED_CUES = (
    "must",
    "required",
    "require",
    "requires",
    "requirement",
    "essential",
    "mandatory",
    "need to have",
    "needs to have",
    "prior experience",
    "proven experience",
    "hands-on experience",
    "strong experience",
    "deep experience",
    "solid experience",
    "experience in",
    "experience with",
    "experience handling",
    "background in",
    "years of",
)


def _contains_word(text: str, phrase: str) -> bool:
    # Lookarounds instead of \b so phrases ending in punctuation ("u.s") still anchor.
    return re.search(rf"(?<!\w){re.escape(phrase)}(?!\w)", text) is not None


def _section_of_header(line: str) -> str:
    """Classify a line as a section header, or '' if it is ordinary prose."""
    stripped = line.strip().strip("-*•#: ").strip()
    if not stripped or len(stripped) > 60:
        return ""
    if not (line.rstrip().endswith(":") or len(stripped.split()) <= 6):
        return ""
    # Preferred wins first: "preferred qualifications" also contains "qualification".
    if any(header in stripped for header in _PREFERRED_HEADERS):
        return "preferred"
    if any(header in stripped for header in _REQUIRED_HEADERS):
        return "required"
    return ""


def _sentences(line: str) -> list[str]:
    return [part for part in re.split(r"[.;•|?!*]\s*", line) if part.strip()]


# Scraped descriptions are often one huge unpunctuated blob, so a cue is only
# trusted when it sits close to the term it supposedly qualifies.
_CUE_WINDOW = 120


def _term_windows(sentence: str, terms: list[str]) -> list[str]:
    windows: list[str] = []
    for term in terms:
        for match in re.finditer(rf"(?<!\w){re.escape(term)}(?!\w)", sentence):
            start = max(0, match.start() - _CUE_WINDOW)
            windows.append(sentence[start : match.end() + _CUE_WINDOW])
    return windows


def domain_required(title: str, description: str, value: str) -> bool:
    """True when any rule term appears as a hard requirement of the posting."""
    terms = [term.strip() for term in (value or "").lower().split(",") if term.strip()]
    if not terms:
        return False
    # A domain word in the title means the role IS the domain, e.g.
    # "Backend Engineer - Healthcare Platform".
    title = (title or "").lower()
    if any(_contains_word(title, term) for term in terms):
        return True
    section = ""
    for line in (description or "").lower().splitlines():
        header = _section_of_header(line)
        if header:
            section = header
            continue
        for sentence in _sentences(line):
            for window in _term_windows(sentence, terms):
                if any(_contains_word(window, cue) for cue in _PREFERRED_CUES):
                    continue
                if any(_contains_word(window, cue) for cue in _REQUIRED_CUES):
                    return True
                if section == "required":
                    return True
    return False


# --- work_authorization matching -----------------------------------------------
# Blocks "must be authorized to work in the U.S." / "we cannot sponsor a visa"
# style eligibility walls. The rule value carries region terms; a region has to
# be mentioned at all, and an authorization cue has to sit near a region term
# (or an explicit sponsorship denial appear anywhere). Softeners like
# "visa sponsorship available" never block.

_SPONSORSHIP_DENIAL_CUES = (
    "unable to sponsor",
    "cannot sponsor",
    "can not sponsor",
    "can't sponsor",
    "not able to sponsor",
    "do not sponsor",
    "don't sponsor",
    "does not sponsor",
    "will not sponsor",
    "won't sponsor",
    "no visa sponsorship",
    "no sponsorship",
    "not offer sponsorship",
    "not offer visa sponsorship",
    "not provide sponsorship",
    "not provide visa sponsorship",
    "sponsorship is not available",
    "sponsorship not available",
    "without sponsorship",
    "not eligible for sponsorship",
)
_AUTH_SOFTENER_CUES = (
    "sponsorship available",
    "sponsorship is available",
    "sponsorship offered",
    "offer visa sponsorship",
    "offer sponsorship",
    "provide visa sponsorship",
    "provide sponsorship",
    "will sponsor",
    "can sponsor",
    "happy to sponsor",
    "visa support",
    "relocation support",
)
# "We do not sponsor visas" from an all-remote company means "work from wherever
# you already live" - the opposite of an eligibility wall.
_GLOBAL_REMOTE_SOFTENERS = (
    "home location",
    "home country",
    "country of residence",
    "country you live",
    "country where you live",
    "any country",
    "almost any country",
    "work from anywhere",
    "anywhere in the world",
    "all-remote",
    "fully remote worldwide",
)
_AUTH_CUES = (
    "authorized to work",
    "authorisation to work",
    "authorization to work",
    "work authorization",
    "work authorisation",
    "legally authorized",
    "legally authorised",
    "right to work",
    "eligible to work",
    "eligibility to work",
    "work permit",
    "work visa",
    "citizen",
    "citizens",
    "citizenship",
    "permanent resident",
    "green card",
    "security clearance",
    "must reside",
    "must be located",
    "must live",
    "must be based",
)


def work_auth_required(title: str, description: str, value: str) -> bool:
    """True when the posting demands work authorization/residency in a rule region."""
    terms = [term.strip() for term in (value or "").lower().split(",") if term.strip()]
    if not terms:
        return False
    title = (title or "").lower()
    description = (description or "").lower()
    region_mentioned = any(
        _contains_word(title, term) or _contains_word(description, term) for term in terms
    )
    if not region_mentioned:
        return False
    # An explicit sponsorship denial means local authorization is a precondition,
    # even when the denial sentence itself does not name the region again - unless
    # the surrounding text says people work from wherever they already live.
    for cue in _SPONSORSHIP_DENIAL_CUES:
        for match in re.finditer(rf"(?<!\w){re.escape(cue)}(?!\w)", description):
            start = max(0, match.start() - _CUE_WINDOW)
            window = description[start : match.end() + _CUE_WINDOW]
            if any(_contains_word(window, softener) for softener in _GLOBAL_REMOTE_SOFTENERS):
                continue
            return True
    for line in description.splitlines():
        for sentence in _sentences(line):
            for window in _term_windows(sentence, terms):
                # Repeated "citizen of ..." is an application-form nationality
                # dropdown, not a requirement.
                if window.count("citizen of") >= 2:
                    continue
                if any(
                    _contains_word(window, cue)
                    for cue in _AUTH_SOFTENER_CUES + _GLOBAL_REMOTE_SOFTENERS
                ):
                    continue
                if any(_contains_word(window, cue) for cue in _AUTH_CUES):
                    return True
    return False


def derive_value(job, kind: str) -> str:
    """Pull the blockable value of `kind` out of a job."""
    if kind == "apply_domain":
        return _apply_host(job)
    if kind == "company":
        return job.company
    if kind == "source":
        return job.discovery_source
    if kind == "job":
        return job.job_slug
    if kind in {"title_pattern", "required_domain", "work_authorization"}:
        raise ValueError(f"{kind} cannot be derived from a job; pass --value explicitly.")
    raise ValueError(f"Unknown feedback kind '{kind}'.")


def summarize(rules: Iterable[FeedbackRule]) -> str:
    rules = list(rules)
    if not rules:
        return "No feedback rules yet."
    lines = []
    by_kind: dict[str, list[FeedbackRule]] = {}
    for rule in rules:
        by_kind.setdefault(rule.kind, []).append(rule)
    for kind in RULE_KINDS:
        entries = by_kind.get(kind)
        if not entries:
            continue
        lines.append(f"\n{kind}  ({RULE_KINDS[kind]})")
        for rule in entries:
            origin = f"  [from {rule.origin_job[:40]}]" if rule.origin_job else ""
            lines.append(f"  {rule.id:<20} {rule.value:<42} {rule.reason}{origin}")
    return "\n".join(lines)
