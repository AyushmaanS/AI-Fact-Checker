import asyncio
import csv
import json
import sys
import time
from pathlib import Path

# Allows `python eval/run_eval.py` to find the app package when the script's own
# directory (not the repo root) is what ends up on sys.path.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.models.schemas import Claim  # noqa: E402
from app.pipeline import MAX_CONCURRENT_RESEARCH, process_claims  # noqa: E402

CASES_PATH = Path(__file__).parent / "cases" / "verdict_examples.json"
RESULTS_PATH = Path(__file__).parent / "results.csv"


def _load_cases() -> list[dict]:
    with open(CASES_PATH) as f:
        return json.load(f)


async def main() -> None:
    cases = _load_cases()
    claims = [
        Claim(
            claim_id=f"eval{i}",
            text=case["text"],
            topic="general",
            specificity="specific",
            verifiability_score=0.9,
        )
        for i, case in enumerate(cases, start=1)
    ]

    print(f"Running {len(claims)} eval cases (up to {MAX_CONCURRENT_RESEARCH} concurrent)...\n")
    start = time.perf_counter()
    verdicts = await process_claims(claims)
    elapsed = time.perf_counter() - start
    verdicts_by_claim_id = {v.claim_id: v for v in verdicts}

    results = []
    for i, case in enumerate(cases, start=1):
        verdict = verdicts_by_claim_id.get(f"eval{i}")
        actual_label = verdict.label if verdict else "ERROR"
        passed = actual_label == case["expected_label"]
        results.append(
            {
                "text": case["text"],
                "expected_label": case["expected_label"],
                "actual_label": actual_label,
                "passed": passed,
            }
        )
        status = "PASS" if passed else "FAIL"
        print(f"[{status}] expected={case['expected_label']:<15} actual={actual_label:<15} {case['text'][:65]!r}")

    passed_count = sum(r["passed"] for r in results)
    total = len(results)
    print(f"\n{'=' * 70}")
    print(f"Pass rate: {passed_count}/{total} ({passed_count / total:.0%})  |  {elapsed:.1f}s total")

    with open(RESULTS_PATH, "w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=["text", "expected_label", "actual_label", "passed"])
        writer.writeheader()
        writer.writerows(results)
    print(f"Results saved to {RESULTS_PATH}")


if __name__ == "__main__":
    asyncio.run(main())
