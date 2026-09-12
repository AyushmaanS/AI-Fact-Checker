import json
from pathlib import Path

import pytest

from app.agents.intent_classifier import classify_intent
from app.config import OPENAI_API_KEY

pytestmark = pytest.mark.skipif(
    not OPENAI_API_KEY,
    reason="OPENAI_API_KEY not set in .env",
)

CASES_PATH = Path(__file__).parent.parent / "eval" / "cases" / "intent_examples.json"


def _load_cases():
    with open(CASES_PATH) as f:
        return json.load(f)


def test_intent_classifier_accuracy():
    cases = _load_cases()
    correct = 0
    misses = []

    for case in cases:
        result = classify_intent(case["text"])
        if result.label == case["expected_label"]:
            correct += 1
        else:
            misses.append((case["text"], case["expected_label"], result.label))

    accuracy = correct / len(cases)
    print(f"\nIntent classifier accuracy: {correct}/{len(cases)} ({accuracy:.0%})")
    for text, expected, actual in misses:
        print(f"  MISS: expected={expected} got={actual} text={text!r}")

    assert correct >= 12, f"Only {correct}/{len(cases)} correct, need >= 12"
