"""Backward-compatible name for the explicitly selected CUDA PEFT provider."""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path
from typing import Optional

from llm.qwen_peft import QwenPEFTProvider, QwenRuntimeConfig

DEFAULT_BASE_MODEL = QwenRuntimeConfig().base_model
DEFAULT_ADAPTER_DIR = QwenRuntimeConfig().adapter_dir
DEFAULT_INPUT_BUDGET = QwenRuntimeConfig().max_input_tokens


class QwenLLM(QwenPEFTProvider):
    """Compatibility wrapper; never falls back to CPU or downloads model files."""

    def __init__(
        self,
        base_model_name: Optional[str] = None,
        adapter_dir: Optional[Path] = None,
        device: Optional[str] = None,
        load_in_8bit: bool = False,
        load_in_4bit: bool = False,
        runtime_config: Optional[QwenRuntimeConfig] = None,
        **kwargs,
    ) -> None:
        if load_in_8bit or load_in_4bit:
            raise ValueError("Quantized Qwen loading is not supported in this deployment configuration.")
        if kwargs:
            unexpected = ", ".join(sorted(kwargs))
            raise TypeError(f"Unexpected QwenLLM options: {unexpected}")
        config = runtime_config or QwenRuntimeConfig.from_environment()
        updates = {}
        if base_model_name:
            updates["base_model"] = base_model_name
        if adapter_dir is not None:
            updates["adapter_dir"] = Path(adapter_dir).expanduser()
        if device is not None:
            updates["device"] = device.casefold()
        if updates:
            config = replace(config, **updates)
        super().__init__(config)
