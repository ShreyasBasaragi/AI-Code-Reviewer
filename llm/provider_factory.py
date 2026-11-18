"""Lazy singleton selection for review and fix-generation providers."""

from __future__ import annotations

import os
import threading
import hashlib
from typing import Any

_providers: dict[str, Any] = {}
_provider_lock = threading.RLock()


def configured_provider_name(task: str = "review") -> str:
    if task not in {"review", "fix"}:
        raise ValueError("Provider task must be 'review' or 'fix'.")
    from llm.provider_settings import PROVIDER_SETTINGS

    snapshot = PROVIDER_SETTINGS.public_snapshot()
    if snapshot["generation"] > 0:
        name = snapshot[f"{task}_provider"]
    elif task == "fix":
        name = os.getenv("AI_CODE_REVIEWER_FIX_PROVIDER", "groq").strip().casefold()
    else:
        name = os.getenv("AI_CODE_REVIEWER_PROVIDER", "groq").strip().casefold()
    return "qwen_local" if name == "qwen" else name


def get_provider(name: str, *, settings: dict[str, Any] | None = None):
    normalized = name.strip().casefold()
    # Preserve the existing explicit `qwen` setting as an alias while changing
    # its unsafe base-only/fallback loader to the validated local PEFT adapter.
    if normalized == "qwen":
        normalized = "qwen_local"
    if normalized not in {"groq", "qwen_local"}:
        raise ValueError("Provider must be one of: groq, qwen_local.")
    settings = dict(settings or {})
    generation = settings.get("generation", "environment")
    secret = settings.get("api_key")
    secret_fingerprint = hashlib.sha256(secret.encode("utf-8")).hexdigest() if isinstance(secret, str) else None
    key = (normalized, generation, settings.get("model"), settings.get("temperature"),
           secret_fingerprint)
    with _provider_lock:
        provider = _providers.get(key)
        if provider is None:
            if normalized == "groq":
                from llm.groq_client import GroqLLM

                provider = GroqLLM(
                    model=settings.get("model"),
                    temperature=settings.get("temperature", 0.1),
                    api_key=settings.get("api_key"),
                    max_output_tokens=settings.get("max_output_tokens"),
                )
            else:
                from llm.qwen_client import QwenLLM
                from dataclasses import replace
                from pathlib import Path
                from llm.qwen_peft import QwenRuntimeConfig

                if not settings:
                    provider = QwenLLM()
                else:
                    config = QwenRuntimeConfig.from_environment()
                    updates = {
                        "base_model": settings.get("base_model", config.base_model),
                        "base_model_id": settings.get("base_model", config.base_model_id),
                        "adapter_dir": Path(settings.get("adapter_path", config.adapter_dir)),
                        "temperature": settings.get("temperature", config.temperature),
                    }
                    if settings.get("max_output_tokens") is not None:
                        updates.update(max_new_tokens=settings["max_output_tokens"],
                                       max_fix_new_tokens=settings["max_output_tokens"])
                    config = replace(config, **updates)
                    provider = QwenLLM(runtime_config=config)
            _providers[key] = provider
        return provider


def get_configured_provider(task: str = "review"):
    from llm.provider_settings import PROVIDER_SETTINGS

    settings = PROVIDER_SETTINGS.task_snapshot(task)
    name = settings.pop("provider")
    if name == "groq" and not settings.get("source_sharing_consent"):
        from llm.provider_settings import ProviderSettingsError

        raise ProviderSettingsError("source_sharing_not_authorized")
    settings["model"] = settings.pop("groq_model")
    settings["base_model"] = settings.pop("qwen_base_model")
    settings["adapter_path"] = settings.pop("qwen_adapter_path")
    settings.pop("review_provider", None)
    settings.pop("fix_provider", None)
    settings.pop("source_sharing_consent", None)
    settings.pop("qwen_quantization", None)
    settings.pop("review_temperature", None)
    settings.pop("fix_temperature", None)
    settings["max_output_tokens"] = settings.pop("max_output_tokens")
    return get_provider(name, settings=settings)


def clear_provider_cache_for_tests() -> None:
    """Drop singleton references; intended for isolated tests only."""
    with _provider_lock:
        _providers.clear()


def invalidate_provider_cache() -> None:
    """Drop cached providers after an authenticated settings generation change."""
    with _provider_lock:
        _providers.clear()
