"""Evaluation of the space-use classifier against a hand-labelled multilingual set.

This measures the *dictionary* classifier only, so the test is deterministic and needs no API key
(good for CI and reproducible thesis numbers). The labelled set (``labelled_spaces.json``) covers
English, Nordic/Finnish, the Finnish apartment notation, BIM measurement overlays, and a mojibake
turning-circle name. Run directly to print the confusion matrix:

    python -m core_backend.tests.test_classifier_eval
"""

import json
import os

from sklearn.metrics import accuracy_score, confusion_matrix

from core_backend.space_classifier import classify_name

LABELLED_PATH = os.path.join(os.path.dirname(__file__), "labelled_spaces.json")
MIN_ACCURACY = 0.85  # dictionary should resolve the labelled set well above chance


def load_cases():
    with open(LABELLED_PATH, "r", encoding="utf-8") as f:
        return json.load(f)


def evaluate():
    """Return (accuracy, y_true, y_pred, labels) for the dictionary classifier."""
    cases = load_cases()
    y_true = [c["expected"] for c in cases]
    y_pred = [classify_name(c.get("long_name"), c.get("name"))[0] for c in cases]
    accuracy = accuracy_score(y_true, y_pred)
    labels = sorted(set(y_true) | set(y_pred))
    return accuracy, y_true, y_pred, labels


def test_classifier_accuracy():
    accuracy, y_true, y_pred, labels = evaluate()
    misses = [(t, p) for t, p in zip(y_true, y_pred) if t != p]
    assert accuracy >= MIN_ACCURACY, f"accuracy {accuracy:.3f} < {MIN_ACCURACY}; misses: {misses}"


if __name__ == "__main__":
    accuracy, y_true, y_pred, labels = evaluate()
    print(f"Dictionary classifier accuracy on labelled set: {accuracy:.3f} ({len(y_true)} cases)\n")

    matrix = confusion_matrix(y_true, y_pred, labels=labels)
    width = max(len(l) for l in labels) + 1
    print("Confusion matrix (rows = true, cols = predicted):")
    print(" " * width + " ".join(f"{l[:6]:>7}" for l in labels))
    for label, row in zip(labels, matrix):
        print(f"{label:<{width}}" + " ".join(f"{v:>7}" for v in row))

    misses = [(c["long_name"], c["name"], t, p)
              for c, t, p in zip(load_cases(), y_true, y_pred) if t != p]
    if misses:
        print(f"\nMisclassified ({len(misses)}):")
        for long_name, name, true, pred in misses:
            print(f"  long_name={long_name!r} name={name!r}: expected {true}, got {pred}")
    else:
        print("\nNo misclassifications.")
