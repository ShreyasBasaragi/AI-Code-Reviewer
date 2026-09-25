"""
CRITIQUE — LLM Evaluation Benchmark
=====================================

Evaluates the fine-tuned Qwen 2.5 Coder 3B LoRA model across a curated
evaluation benchmark of code samples (vulnerable, buggy, and clean).

Computes quantitative evaluation metrics:
  - Issue Detection Accuracy
  - Precision, Recall, and F1-Score
  - Vulnerability Classification Match Rate
  - False Positive Rate & False Negative Rate
  - Average Inference Latency (seconds per review)
"""

import os
os.environ["USE_TF"] = "0"
os.environ["USE_TORCH"] = "1"

import sys
import time
import json
import logging
from dataclasses import dataclass
from typing import List, Dict, Any

# Ensure line buffering and utf-8 encoding
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)-7s | %(message)s",
    datefmt="%H:%M:%S"
)
logger = logging.getLogger("evaluate_llm")

# ─────────────────────────────────────────────────────────────
# BENCHMARK DATASET (Ground Truth Annotated)
# ─────────────────────────────────────────────────────────────

BENCHMARK_SUITE = [
    # --- POSITIVE CASES (Known Bugs / Vulnerabilities) ---
    {
        "id": "SEC-01",
        "name": "SQL Injection via Concatenation",
        "has_issue": True,
        "expected_type": "Security",
        "expected_severity": "High",
        "code": """def get_user_records(user_input):
    query = "SELECT * FROM accounts WHERE username = '" + user_input + "'"
    cursor = db.cursor()
    cursor.execute(query)
    return cursor.fetchall()"""
    },
    {
        "id": "BUG-01",
        "name": "Mutable Default Argument",
        "has_issue": True,
        "expected_type": "Bug",
        "expected_severity": "Medium",
        "code": """def register_student(name, enrolled_courses=[]):
    enrolled_courses.append(name)
    return enrolled_courses"""
    },
    {
        "id": "RES-01",
        "name": "Unclosed File Descriptor Leak",
        "has_issue": True,
        "expected_type": "Resource",
        "expected_severity": "Medium",
        "code": """def load_credentials(file_path):
    f = open(file_path, 'r')
    data = json.load(f)
    return data"""
    },
    {
        "id": "SEC-02",
        "name": "Hardcoded API Secret Key",
        "has_issue": True,
        "expected_type": "Security",
        "expected_severity": "High",
        "code": """API_SECRET_KEY = "sk-live-998877665544332211aabbcc"
def authenticate_request():
    return client.connect(api_key=API_SECRET_KEY)"""
    },
    {
        "id": "BUG-02",
        "name": "Bare Except Swallowing Errors",
        "has_issue": True,
        "expected_type": "Bug",
        "expected_severity": "Medium",
        "code": """def save_user_profile(user_data):
    try:
        db.insert(user_data)
    except:
        pass"""
    },
    {
        "id": "LOG-01",
        "name": "Off-by-One Pagination Indexing",
        "has_issue": True,
        "expected_type": "Bug",
        "expected_severity": "High",
        "code": """def paginate(items, page_size, page_number):
    start = page_number * page_size
    end = start + page_size
    return items[start:end]"""
    },

    # --- NEGATIVE CASES (Clean, Well-Formed Code) ---
    {
        "id": "CLEAN-01",
        "name": "Safe Parameterized SQL Query",
        "has_issue": False,
        "expected_type": "None",
        "expected_severity": "None",
        "code": """def get_user_records_safe(user_input):
    query = "SELECT * FROM accounts WHERE username = %s"
    cursor = db.cursor()
    cursor.execute(query, (user_input,))
    return cursor.fetchall()"""
    },
    {
        "id": "CLEAN-02",
        "name": "Clean Math Utility with Validation",
        "has_issue": False,
        "expected_type": "None",
        "expected_severity": "None",
        "code": """def calculate_circle_area(radius: float) -> float:
    \"\"\"Calculate area of circle given positive radius.\"\"\"
    if radius < 0:
        raise ValueError("Radius must be non-negative")
    return 3.141592653589793 * (radius ** 2)"""
    },
    {
        "id": "CLEAN-03",
        "name": "Safe Context Manager File Handling",
        "has_issue": False,
        "expected_type": "None",
        "expected_severity": "None",
        "code": """def load_config_safe(file_path: str) -> dict:
    with open(file_path, "r", encoding="utf-8") as f:
        return json.load(f)"""
    },
]


def evaluate_model():
    print("=" * 75)
    print("       CRITIQUE — LLM QUANTITATIVE EVALUATION BENCHMARK")
    print("   Evaluating Fine-Tuned Qwen 2.5 Coder 3B with RAG Augmentation")
    print("=" * 75)

    from rag.retrieve import retrieve_context
    from llm.qwen_client import QwenLLM
    from mcp_server.notifier import parse_review_verdict

    print(f"\n[INIT] Loading fine-tuned Qwen 2.5 Coder 3B LoRA...")
    t_load_start = time.time()
    llm = QwenLLM()
    t_load = time.time() - t_load_start
    print(f"[INIT] Model loaded on {llm.device} in {t_load:.1f}s.\n")

    print(f"Benchmark test suite: {len(BENCHMARK_SUITE)} test cases (6 buggy, 3 clean)\n")

    results = []
    tp = fp = tn = fn = 0
    type_matches = 0
    total_latency = 0.0

    print("-" * 75)
    print(f"{'ID':<10} | {'Test Case':<32} | {'Ground Truth':<12} | {'Prediction':<12} | {'Latency':<7}")
    print("-" * 75)

    for tc in BENCHMARK_SUITE:
        t0 = time.time()

        # 1. RAG retrieval
        context = retrieve_context(tc["code"], k=3)

        # 2. LLM inference
        review = llm.analyze_code(code=tc["code"], context=context)
        latency = time.time() - t0
        total_latency += latency

        # 3. Parse verdict
        verdict = parse_review_verdict(review)
        pred_issue = verdict["issue_found"]
        pred_type = verdict["issue_type"]
        pred_sev = verdict["severity"]

        # 4. Confusion matrix calculation
        gt_issue = tc["has_issue"]
        if gt_issue and pred_issue:
            tp += 1
            classification_status = "TP [Hit]"
        elif not gt_issue and not pred_issue:
            tn += 1
            classification_status = "TN [Clean]"
        elif not gt_issue and pred_issue:
            fp += 1
            classification_status = "FP [Overflag]"
        else:
            fn += 1
            classification_status = "FN [Miss]"

        # Check type match if bug was detected
        if gt_issue and pred_issue:
            expected_type_lower = tc["expected_type"].lower()
            pred_type_lower = pred_type.lower()
            if expected_type_lower in pred_type_lower or pred_type_lower in expected_type_lower:
                type_matches += 1

        gt_str = "Issue (Yes)" if gt_issue else "Clean (No)"
        pred_str = "Issue (Yes)" if pred_issue else "Clean (No)"

        print(f"{tc['id']:<10} | {tc['name'][:32]:<32} | {gt_str:<12} | {pred_str:<12} | {latency:5.1f}s")

        results.append({
            "id": tc["id"],
            "name": tc["name"],
            "ground_truth": gt_issue,
            "predicted": pred_issue,
            "status": classification_status,
            "pred_type": pred_type,
            "pred_severity": pred_sev,
            "latency": latency,
            "review": review[:200]
        })

    # ── METRIC CALCULATIONS ──────────────────────────────────
    total_samples = len(BENCHMARK_SUITE)
    accuracy = (tp + tn) / total_samples if total_samples > 0 else 0
    precision = tp / (tp + fp) if (tp + fp) > 0 else 0
    recall = tp / (tp + fn) if (tp + fn) > 0 else 0
    f1 = (2 * precision * recall) / (precision + recall) if (precision + recall) > 0 else 0
    avg_latency = total_latency / total_samples if total_samples > 0 else 0
    category_acc = (type_matches / tp) * 100 if tp > 0 else 0

    print("\n" + "=" * 75)
    print("                    EVALUATION METRICS REPORT")
    print("=" * 75)
    print(f"  • Total Test Samples        : {total_samples}")
    print(f"  • True Positives (TP)       : {tp}  (Bugs correctly caught)")
    print(f"  • True Negatives (TN)       : {tn}  (Clean code accepted)")
    print(f"  • False Positives (FP)      : {fp}  (Clean code overflagged)")
    print(f"  • False Negatives (FN)      : {fn}  (Bugs missed)")
    print("-" * 75)
    print(f"  [METRIC] Accuracy           : {accuracy * 100:.1f}%")
    print(f"  [METRIC] Precision          : {precision * 100:.1f}%")
    print(f"  [METRIC] Recall (Detection) : {recall * 100:.1f}%")
    print(f"  [METRIC] F1-Score           : {f1:.3f}")
    print(f"  [METRIC] Category Match Rate: {category_acc:.1f}%")
    print(f"  [METRIC] Avg Inference Time : {avg_latency:.2f} seconds / sample")
    print("=" * 75)

    # Save metrics report
    report_file = "benchmark_scores.json"
    with open(report_file, "w", encoding="utf-8") as f:
        json.dump({
            "metrics": {
                "accuracy": round(accuracy * 100, 2),
                "precision": round(precision * 100, 2),
                "recall": round(recall * 100, 2),
                "f1_score": round(f1, 3),
                "category_match_rate": round(category_acc, 2),
                "avg_latency_seconds": round(avg_latency, 2),
                "confusion_matrix": {"tp": tp, "tn": tn, "fp": fp, "fn": fn}
            },
            "cases": results
        }, f, indent=2)
    print(f"\nDetailed metrics saved to: {report_file}")


if __name__ == "__main__":
    evaluate_model()
