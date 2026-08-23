"""Turn a free-text rejection reason into proposed feedback rules.

The model does extraction, never judgment: it maps a sentence like "canonical's
work culture is bad" onto the existing rule dimensions and reports what it found.
Nothing is written to `feedback.json` here - the caller shows the proposals with
their blast radius and the human confirms.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass, field

from .feedback import RULE_KINDS, FeedbackStore, _apply_host, derive_value
from .llm import generate_json_completion, resolve_llm_config
from .models import JobRecord

SYSTEM_PROMPT = """You extract structured job-filter rules from a recruiter's plain-English \
rejection reason. You never invent preferences the reason does not state.

Available rule kinds:
- apply_domain: block the host of the apply URL. Use when the reason is about the SITE \
being a reposter, aggregator, scraper, or otherwise not the real employer.
- company: block the employer by name. Use when the reason is about the COMPANY itself \
(culture, reputation, industry, past experience, "nothing from them").
- title_pattern: block a short phrase appearing in job titles. Use when the reason is \
about the KIND OF ROLE (e.g. mentoring, analyst, support, QA). Keep the phrase 1-3 words, \
lowercase, and general enough to match future postings.
- source: block an entire discovery source. Only when the reason explicitly blames the source.
- required_domain: block postings that demand a specific business domain as a hard \
requirement. Use when the reason is about DOMAIN EXPERTISE the posting insists on \
(healthcare, insurance, defense, gambling, adtech, ...). value is a comma-separated list \
of 5-10 lowercase terms covering the domain's name and its jargon as it appears in \
postings (e.g. "healthcare, health care, healthtech, ehr, emr, hipaa, clinical, medical \
claims"). Ground the terms in the description excerpt when one is provided. Avoid \
acronyms that collide with unrelated tech ("emr" also means Amazon EMR, "pos" means \
point of sale AND part of speech); use unambiguous multi-word forms like "electronic \
medical records" instead. This kind only blocks postings that list a term as a \
MUST-HAVE; postings mentioning the domain as a bonus or preferred qualification still \
pass, so prefer it over "job" whenever the reason is a domain mismatch - it is safe by \
construction.
- work_authorization: block postings that demand work authorization or residency in a \
region, or refuse visa sponsorship there. Use when the reason is about being INELIGIBLE \
to work somewhere (visa, sponsorship, citizenship, residency, "must be authorized to \
work in..."). value is a comma-separated list of lowercase region terms and aliases, \
e.g. "u.s, usa, united states, america, us citizens, us citizenship, us work \
authorization, us-based". Never use the bare word "us" (it collides with the pronoun); \
embed it in phrases like "us citizenship" or use "u.s"/"usa" instead. Cover every \
region the reason names (e.g. also "eu, european union, canada, canadian, australia"). \
This kind only blocks when a region term appears near authorization/sponsorship \
wording, so ordinary mentions of a country still pass.
- job: block only this one posting. Use when the reason is specific to this listing and \
would be wrong to generalize (a one-off, a broken scrape, bad timing).

Rules:
1. Return the smallest set of rules that captures the reason. Usually exactly one.
2. Prefer the NARROWEST kind that still generalizes. If the reason is about this posting \
only, use "job".
3. For apply_domain, return a registrable-ish suffix that covers subdomains \
(e.g. "up.railway.app", not "remotezest.up.railway.app") when the whole host family is junk.
4. Never propose a rule the reason does not support. If the reason is too vague to \
generalize, return a single "job" rule.
5. confidence is "high", "medium", or "low".

Respond with JSON only:
{"proposals": [{"kind": "...", "value": "...", "reason": "<short canonical reason>", \
"confidence": "high|medium|low", "explanation": "<why this kind, one sentence>"}]}"""


@dataclass
class RuleProposal:
    kind: str
    value: str
    reason: str
    confidence: str = "medium"
    explanation: str = ""
    blast_radius: int = 0
    already_exists: bool = False

    def to_dict(self) -> dict:
        return {
            "kind": self.kind,
            "value": self.value,
            "reason": self.reason,
            "confidence": self.confidence,
            "explanation": self.explanation,
            "blast_radius": self.blast_radius,
            "already_exists": self.already_exists,
        }


@dataclass
class ProposalResult:
    proposals: list[RuleProposal] = field(default_factory=list)
    llm_used: bool = True
    error: str = ""

    def to_dict(self) -> dict:
        return {
            "proposals": [p.to_dict() for p in self.proposals],
            "llm_used": self.llm_used,
            "error": self.error,
        }


def _job_context(job: JobRecord) -> str:
    lines = [
        f"company: {job.company}",
        f"title: {job.title}",
        f"apply_host: {_apply_host(job) or '(none)'}",
        f"discovery_source: {job.discovery_source}",
        f"location: {job.location_raw or '(none)'}",
        f"remote_scope: {job.remote_scope}",
        f"job_slug: {job.job_slug}",
    ]
    # The excerpt lets the model ground required_domain terms in the posting's
    # actual jargon instead of guessing synonyms.
    description = " ".join((job.description_text or "").split())
    if description:
        lines.append(f"description_excerpt: {description[:1500]}")
    return "\n".join(lines)


def blast_radius(connection: sqlite3.Connection, kind: str, value: str) -> int:
    """How many stored jobs a candidate rule would block, so the human sees the damage first."""
    # Build through add() so the value is normalized exactly as runtime matching expects.
    probe = FeedbackStore([])
    try:
        probe.add(kind, value, "probe")
    except ValueError:
        return 0
    rows = connection.execute("SELECT payload_json FROM jobs").fetchall()
    count = 0
    for row in rows:
        try:
            job = JobRecord.model_validate_json(row["payload_json"])
        except Exception:
            continue
        if probe.match(job):
            count += 1
    return count


def fallback_proposals(job: JobRecord, reason: str) -> list[RuleProposal]:
    """Heuristic proposals for when no LLM key is configured or the call fails.

    Deliberately conservative: it only fires on unambiguous keyword signals and
    otherwise proposes blocking this single job.
    """
    lowered = reason.lower()
    proposals: list[RuleProposal] = []

    site_words = ("reposter", "repost", "aggregator", "scraper", "not the employer", "spam", "fake site")
    company_words = ("culture", "company", "them", "employer", "reputation", "glassdoor")

    host = _apply_host(job)
    if host and any(word in lowered for word in site_words):
        proposals.append(
            RuleProposal(kind="apply_domain", value=host, reason=reason.strip()[:120],
                         confidence="medium", explanation="Reason mentions the site rather than the role.")
        )
    elif any(word in lowered for word in company_words):
        proposals.append(
            RuleProposal(kind="company", value=job.company, reason=reason.strip()[:120],
                         confidence="medium", explanation="Reason mentions the company itself.")
        )

    if not proposals:
        proposals.append(
            RuleProposal(kind="job", value=job.job_slug, reason=reason.strip()[:120],
                         confidence="low", explanation="Reason is not clearly generalizable; blocking this posting only.")
        )
    return proposals


def propose_rules(
    job: JobRecord,
    reason: str,
    *,
    connection: sqlite3.Connection | None = None,
    store: FeedbackStore | None = None,
    provider: str | None = None,
    model: str | None = None,
) -> ProposalResult:
    """Map a free-text reason onto candidate feedback rules. Never writes."""
    reason = (reason or "").strip()
    if not reason:
        return ProposalResult(proposals=[], llm_used=False, error="Empty reason.")

    llm_used = True
    error = ""
    try:
        config = resolve_llm_config(provider=provider, model=model, max_output_tokens=700)
        payload = generate_json_completion(
            config=config,
            system_prompt=SYSTEM_PROMPT,
            user_prompt=f"Job:\n{_job_context(job)}\n\nRejection reason:\n{reason}",
        )
        raw = payload.get("proposals") or []
        proposals = [
            RuleProposal(
                kind=str(entry.get("kind", "")).strip(),
                value=str(entry.get("value", "")).strip(),
                reason=str(entry.get("reason") or reason).strip()[:160],
                confidence=str(entry.get("confidence", "medium")).strip().lower(),
                explanation=str(entry.get("explanation", "")).strip(),
            )
            for entry in raw
            if str(entry.get("kind", "")).strip() in RULE_KINDS
        ]
        # A model that returns nothing usable must not silently drop the rejection.
        if not proposals:
            proposals = fallback_proposals(job, reason)
            llm_used = False
            error = "Model returned no usable proposal; used heuristic fallback."
    except Exception as exc:  # missing key, network failure, malformed JSON
        proposals = fallback_proposals(job, reason)
        llm_used = False
        error = f"{type(exc).__name__}: {exc}"

    # A "job" proposal always needs the real slug, whatever the model returned.
    for proposal in proposals:
        if proposal.kind == "job":
            proposal.value = job.job_slug
        elif not proposal.value:
            try:
                proposal.value = derive_value(job, proposal.kind)
            except ValueError:
                proposal.value = job.job_slug
                proposal.kind = "job"

    if connection is not None:
        for proposal in proposals:
            proposal.blast_radius = blast_radius(connection, proposal.kind, proposal.value)
    if store is not None:
        for proposal in proposals:
            normalized = FeedbackStore([])
            try:
                candidate = normalized.add(proposal.kind, proposal.value, "")
            except ValueError:
                continue
            proposal.already_exists = any(
                rule.kind == candidate.kind and rule.value == candidate.value for rule in store.rules
            )

    return ProposalResult(proposals=proposals, llm_used=llm_used, error=error)
