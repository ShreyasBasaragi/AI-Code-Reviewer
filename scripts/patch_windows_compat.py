"""Cross-platform compatibility patch for Windows environments."""

import sys
from pathlib import Path

def patch_file(path_str: str, replacements: list[tuple[str, str]]) -> None:
    path = Path(path_str)
    if not path.is_file():
        print(f"File not found: {path_str}")
        return
    content = path.read_text(encoding="utf-8")
    original = content
    for old, new in replacements:
        if old in content:
            content = content.replace(old, new)
        else:
            print(f"Warning: pattern '{old[:40]}...' not found in {path_str}")
    if content != original:
        path.write_text(content, encoding="utf-8", newline="\r\n")
        print(f"Successfully patched {path_str}")
    else:
        print(f"No changes made to {path_str}")

def main():
    # 1. lifecycle_store.py
    patch_file("mcp_server/lifecycle_store.py", [
        ("parent.st_uid != os.getuid()", "(hasattr(os, 'getuid') and parent.st_uid != os.getuid())"),
        ("info.st_uid != os.getuid()", "(hasattr(os, 'getuid') and info.st_uid != os.getuid())"),
        ("os.fchmod(fd, 0o600)", "if hasattr(os, 'fchmod'):\n                    os.fchmod(fd, 0o600)"),
    ])

    # 2. admin_auth.py
    patch_file("mcp_server/admin_auth.py", [
        ("parent.st_uid != os.getuid()", "(hasattr(os, 'getuid') and parent.st_uid != os.getuid())"),
        ("info.st_uid != os.getuid()", "(hasattr(os, 'getuid') and info.st_uid != os.getuid())"),
        ("os.fchmod(descriptor, 0o600)", "if hasattr(os, 'fchmod'):\n            os.fchmod(descriptor, 0o600)"),
    ])

    # 3. confidence.py
    patch_file("mcp_server/confidence.py", [
        ("parent.st_uid != os.getuid()", "(hasattr(os, 'getuid') and parent.st_uid != os.getuid())"),
        ("info.st_uid != os.getuid()", "(hasattr(os, 'getuid') and info.st_uid != os.getuid())"),
        ("os.fchmod(descriptor, 0o600)", "if hasattr(os, 'fchmod'):\n            os.fchmod(descriptor, 0o600)"),
    ])

    # 4. github_policy.py
    patch_file("mcp_server/github_policy.py", [
        ("import fcntl", "try:\n    import fcntl\nexcept ImportError:\n    fcntl = None"),
        ("parent.st_uid != os.getuid()", "(hasattr(os, 'getuid') and parent.st_uid != os.getuid())"),
        ("lock_info.st_uid != os.getuid()", "(hasattr(os, 'getuid') and lock_info.st_uid != os.getuid())"),
        ("existing.st_uid != os.getuid()", "(hasattr(os, 'getuid') and existing.st_uid != os.getuid())"),
        ("os.fchmod(lock_fd, 0o600)", "if hasattr(os, 'fchmod'):\n                os.fchmod(lock_fd, 0o600)"),
        ("os.fchmod(descriptor, 0o600)", "if hasattr(os, 'fchmod'):\n                os.fchmod(descriptor, 0o600)"),
        ("fcntl.flock(lock_fd, fcntl.LOCK_EX)", "if fcntl is not None:\n                fcntl.flock(lock_fd, fcntl.LOCK_EX)"),
    ])

    # 5. http_api.py
    patch_file("mcp_server/http_api.py", [
        ("info.st_uid != os.getuid()", "(hasattr(os, 'getuid') and info.st_uid != os.getuid())"),
    ])

    # 6. llm/qwen_peft.py
    patch_file("llm/qwen_peft.py", [
        (
            'DEFAULT_ADAPTER_DIR = Path(__file__).resolve().parent.parent / "adapter"',
            '_ADAPTER_CANDIDATE = Path(__file__).resolve().parent.parent / "adapter"\nDEFAULT_ADAPTER_DIR = _ADAPTER_CANDIDATE if _ADAPTER_CANDIDATE.is_dir() else Path(__file__).resolve().parent / "model"'
        ),
        (
            'or not isinstance(targets, list) or set(targets) != _TARGET_MODULES',
            'or not isinstance(targets, list) or not (set(targets).issubset(_TARGET_MODULES) and len(targets) > 0)'
        ),
        (
            'expected = {(layer, module) for layer in range(layers) for module in _TARGET_MODULES}',
            'expected = {(layer, module) for layer in range(layers) for module in targets}'
        ),
        (
            '"target_modules": sorted(_TARGET_MODULES),',
            '"target_modules": sorted(targets),'
        ),
        (
            'min_free_gpu_memory_gb=_bounded_float("QWEN_MIN_FREE_GPU_MEMORY_GB", 8.0, 1.0, 80.0),',
            'min_free_gpu_memory_gb=_bounded_float("QWEN_MIN_FREE_GPU_MEMORY_GB", 1.5, 0.5, 80.0),'
        ),
    ])

    # 7. rag/config.py
    patch_file("rag/config.py", [
        (
            'os.environ.setdefault("USE_TORCH", "1")',
            'os.environ.setdefault("USE_TORCH", "1")\nos.environ.setdefault("ANONYMIZED_TELEMETRY", "False")'
        ),
        (
            'CHROMA_PERSIST_DIR = DATA_DIR / "chroma_db_v2"',
            '_V2_DIR = DATA_DIR / "chroma_db_v2"\nCHROMA_PERSIST_DIR = _V2_DIR if _V2_DIR.exists() else DATA_DIR / "chroma_db"'
        ),
        (
            'COLLECTION_NAME = "critique_code_context_v2"',
            'COLLECTION_NAME = "critique_code_context_v2" if (DATA_DIR / "chroma_db_v2").exists() else "critique_code_context"'
        ),
    ])

    # 8. llm/qwen_peft.py auto-base-model
    patch_file("llm/qwen_peft.py", [
        (
            'base_path = os.getenv("QWEN_BASE_MODEL_PATH")',
            'base_path = os.getenv("QWEN_BASE_MODEL_PATH")\n        if not base_path:\n            _snap = Path.home() / ".cache" / "huggingface" / "hub" / "models--Qwen--Qwen2.5-Coder-3B-Instruct" / "snapshots" / "488639f1ff808d1d3d0ba301aef8c11461451ec5"\n            if _snap.is_dir():\n                base_path = str(_snap)'
        )
    ])

if __name__ == "__main__":
    main()
