import os
os.environ.setdefault("USE_TF", "0")
os.environ.setdefault("USE_TORCH", "1")
import logging
from pathlib import Path
from typing import List, Optional

import torch
from transformers import AutoTokenizer, AutoModelForCausalLM
from peft import PeftModel

from llm.base import BaseLLM
from rag.augment import augment_context

logger = logging.getLogger(__name__)

DEFAULT_BASE_MODEL = "Qwen/Qwen2.5-Coder-3B-Instruct"
DEFAULT_ADAPTER_DIR = Path(__file__).resolve().parent / "model"


class QwenLLM(BaseLLM):
    """
    Critique LLM implementation powered by fine-tuned Qwen 2.5 Coder 3B with LoRA.
    """

    def __init__(
        self,
        base_model_name: str = DEFAULT_BASE_MODEL,
        adapter_dir: Optional[Path] = None,
        device: Optional[str] = None,
        load_in_8bit: bool = False,
        load_in_4bit: bool = False,
    ) -> None:
        self.base_model_name = base_model_name
        self.adapter_dir = adapter_dir or DEFAULT_ADAPTER_DIR

        # Setup compute device
        if device:
            self.device = device
        else:
            self.device = "cuda" if torch.cuda.is_available() else "cpu"

        logger.info(f"Initializing QwenLLM on device: {self.device}")
        logger.info(f"Base model: {self.base_model_name}")
        logger.info(f"LoRA Adapter directory: {self.adapter_dir}")

        # Choose optimal dtype
        if self.device == "cuda":
            # bfloat16 is optimal on Ampere (RTX 3000+) or newer, float16 as fallback
            dtype = torch.bfloat16 if torch.cuda.is_bf16_supported() else torch.float16
        else:
            dtype = torch.float32

        # 1. Load Tokenizer
        tokenizer_path = self.base_model_name
        logger.info(f"Loading tokenizer from: {tokenizer_path}")
        self.tokenizer = AutoTokenizer.from_pretrained(
            tokenizer_path,
            trust_remote_code=True,
            padding_side="left"
        )
        if self.tokenizer.pad_token is None:
            self.tokenizer.pad_token = self.tokenizer.eos_token

        # 2. Load Base Model
        logger.info(f"Loading base model '{self.base_model_name}'...")
        
        use_4bit = load_in_4bit
        if self.device == "cuda" and not load_in_8bit and not load_in_4bit:
            vram_gb = torch.cuda.get_device_properties(0).total_memory / (1024**3)
            if vram_gb < 6.0:
                logger.info(f"Detected GPU with {vram_gb:.1f}GB VRAM (< 6GB). Enabling 4-bit NF4 quantization to fit comfortably in VRAM.")
                use_4bit = True

        model_kwargs = {
            "trust_remote_code": True,
        }

        if self.device == "cuda":
            model_kwargs["device_map"] = "auto"
            if use_4bit:
                from transformers import BitsAndBytesConfig
                model_kwargs["quantization_config"] = BitsAndBytesConfig(
                    load_in_4bit=True,
                    bnb_4bit_compute_dtype=torch.float16,
                    bnb_4bit_quant_type="nf4",
                    bnb_4bit_use_double_quant=True,
                )
            elif load_in_8bit:
                model_kwargs["load_in_8bit"] = True
            else:
                model_kwargs["torch_dtype"] = dtype
        else:
            model_kwargs["torch_dtype"] = dtype

        base_model = AutoModelForCausalLM.from_pretrained(
            self.base_model_name,
            **model_kwargs
        )

        # 3. Load Fine-Tuned LoRA Adapter
        if (self.adapter_dir / "adapter_model.safetensors").exists() or (self.adapter_dir / "adapter_model.bin").exists():
            logger.info(f"Loading PEFT LoRA adapter from: {self.adapter_dir}")
            self.model = PeftModel.from_pretrained(
                base_model,
                str(self.adapter_dir),
                is_trainable=False
            )
        else:
            logger.warning(f"No adapter weights found at {self.adapter_dir}. Using base model.")
            self.model = base_model

        if self.device != "cuda" or "device_map" not in model_kwargs:
            self.model.to(self.device)

        self.model.eval()
        logger.info("QwenLLM initialized and ready for code review inference.")

    def _build_messages(self, code: str, context: List[str]) -> List[dict]:
        """Construct conversation messages with RAG context."""
        augmented_prompt = augment_context(code, context)

        system_instruction = (
            "You are Critique, an expert autonomous AI code reviewer.\n"
            "Analyze the given code or pull request diff precisely.\n"
            "Use the retrieved historical review context and style rules as supporting evidence, "
            "but make your own independent engineering judgment.\n\n"
            "Always format your review strictly using these sections:\n"
            "Issue Found: Yes or No\n"
            "Issue Type: Bug / Security / Performance / Style / Refactoring / Readability / None\n"
            "Severity: Critical / High / Medium / Low / None\n"
            "Explanation: <clear explanation of what is wrong or why it is acceptable>\n"
            "Evidence: <reference the specific line or construct in the code>\n"
            "Recommendation: <actionable guidance and corrected code example>"
        )

        messages = [
            {"role": "system", "content": system_instruction},
            {"role": "user", "content": augmented_prompt}
        ]
        return messages

    def analyze_code(
        self,
        code: str,
        context: List[str],
        max_new_tokens: int = 512,
        temperature: float = 0.2,
        top_p: float = 0.9,
    ) -> str:
        """
        Analyze code using retrieved RAG context and the fine-tuned Qwen model.
        """
        if not code or not code.strip():
            return "No code provided for review."

        messages = self._build_messages(code, context)
        prompt_text = self.tokenizer.apply_chat_template(
            messages,
            tokenize=False,
            add_generation_prompt=True
        )

        inputs = self.tokenizer(prompt_text, return_tensors="pt").to(self.model.device)

        with torch.no_grad():
            outputs = self.model.generate(
                **inputs,
                max_new_tokens=max_new_tokens,
                temperature=temperature,
                top_p=top_p,
                do_sample=temperature > 0,
                pad_token_id=self.tokenizer.pad_token_id,
                eos_token_id=self.tokenizer.eos_token_id,
            )

        # Slice off input prompt tokens so only generated tokens are decoded
        input_len = inputs["input_ids"].shape[1]
        generated_tokens = outputs[0][input_len:]
        response_text = self.tokenizer.decode(generated_tokens, skip_special_tokens=True)

        return response_text.strip()
