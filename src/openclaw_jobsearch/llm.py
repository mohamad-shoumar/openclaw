from __future__ import annotations

import json
import os
from dataclasses import dataclass
from typing import Any, Literal
from urllib import error, request

ProviderName = Literal["openai", "anthropic"]

DEFAULT_MODELS: dict[ProviderName, str] = {
    "openai": "gpt-4.1-mini",
    "anthropic": "claude-sonnet-4-6",
}


@dataclass(frozen=True)
class LlmConfig:
    provider: ProviderName
    model: str
    api_key: str
    temperature: float = 0.2
    max_output_tokens: int = 2500


def resolve_llm_config(
    *,
    provider: str | None = None,
    model: str | None = None,
    temperature: float | None = None,
    max_output_tokens: int | None = None,
) -> LlmConfig:
    chosen_provider = _resolve_provider(provider)
    env_key_name = "OPENAI_API_KEY" if chosen_provider == "openai" else "ANTHROPIC_API_KEY"
    api_key = os.environ.get(env_key_name, "").strip()
    if not api_key:
        raise ValueError(f"Missing required API key: {env_key_name}")

    resolved_model = (
        model
        or os.environ.get("OPENCLAW_LLM_MODEL")
        or DEFAULT_MODELS[chosen_provider]
    )
    resolved_temperature = (
        temperature
        if temperature is not None
        else float(os.environ.get("OPENCLAW_LLM_TEMPERATURE", "0.2"))
    )
    resolved_max_tokens = (
        max_output_tokens
        if max_output_tokens is not None
        else int(os.environ.get("OPENCLAW_LLM_MAX_TOKENS", "2500"))
    )
    return LlmConfig(
        provider=chosen_provider,
        model=resolved_model,
        api_key=api_key,
        temperature=resolved_temperature,
        max_output_tokens=resolved_max_tokens,
    )


def generate_json_completion(
    *,
    config: LlmConfig,
    system_prompt: str,
    user_prompt: str,
) -> dict[str, Any]:
    if config.provider == "openai":
        response_payload = _post_json(
            url="https://api.openai.com/v1/chat/completions",
            headers={
                "Authorization": f"Bearer {config.api_key}",
                "Content-Type": "application/json",
            },
            payload={
                "model": config.model,
                "temperature": config.temperature,
                "max_tokens": config.max_output_tokens,
                "response_format": {"type": "json_object"},
                "messages": [
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": user_prompt},
                ],
            },
        )
        content = response_payload["choices"][0]["message"]["content"]
        return _parse_json_content(content)

    response_payload = _post_json(
        url="https://api.anthropic.com/v1/messages",
        headers={
            "x-api-key": config.api_key,
            "anthropic-version": "2023-06-01",
            "content-type": "application/json",
        },
        payload={
            "model": config.model,
            "system": system_prompt,
            "temperature": config.temperature,
            "max_tokens": config.max_output_tokens,
            "messages": [
                {
                    "role": "user",
                    "content": user_prompt,
                }
            ],
        },
    )
    text_blocks = [
        block.get("text", "")
        for block in response_payload.get("content", [])
        if block.get("type") == "text"
    ]
    return _parse_json_content("\n".join(text_blocks))


def _resolve_provider(provider: str | None) -> ProviderName:
    chosen = (provider or os.environ.get("OPENCLAW_LLM_PROVIDER") or "").strip().lower()
    if chosen:
        if chosen not in {"openai", "anthropic"}:
            raise ValueError(f"Unsupported LLM provider: {chosen}")
        return chosen  # type: ignore[return-value]

    has_openai = bool(os.environ.get("OPENAI_API_KEY"))
    has_anthropic = bool(os.environ.get("ANTHROPIC_API_KEY"))
    if has_openai:
        return "openai"
    if has_anthropic:
        return "anthropic"
    raise ValueError(
        "No LLM provider configured. Set OPENAI_API_KEY or ANTHROPIC_API_KEY, "
        "or pass --provider explicitly."
    )


def _post_json(*, url: str, headers: dict[str, str], payload: dict[str, Any]) -> dict[str, Any]:
    raw_request = request.Request(
        url=url,
        data=json.dumps(payload).encode("utf-8"),
        headers=headers,
        method="POST",
    )
    try:
        with request.urlopen(raw_request, timeout=120) as response:
            return json.loads(response.read().decode("utf-8"))
    except error.HTTPError as exc:
        body = exc.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"LLM request failed with HTTP {exc.code}: {body}") from exc
    except error.URLError as exc:
        raise RuntimeError(f"LLM request failed: {exc.reason}") from exc


def _parse_json_content(content: Any) -> dict[str, Any]:
    if isinstance(content, list):
        text = "\n".join(
            item.get("text", "") if isinstance(item, dict) else str(item)
            for item in content
        )
    else:
        text = str(content)
    cleaned = _strip_code_fences(text)
    try:
        parsed = json.loads(cleaned)
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"LLM did not return valid JSON: {cleaned[:400]}") from exc
    if not isinstance(parsed, dict):
        raise RuntimeError("LLM JSON response must be an object.")
    return parsed


def _strip_code_fences(text: str) -> str:
    stripped = text.strip()
    if stripped.startswith("```"):
        stripped = stripped.split("\n", 1)[1] if "\n" in stripped else stripped
        if stripped.endswith("```"):
            stripped = stripped[:-3]
    return stripped.strip()
