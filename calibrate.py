#!/usr/bin/env python3
"""Fit temperature scaling for the model's probabilities on the labelled alerts.

A System One model can rank answers well and still be over- or under-confident. Temperature
scaling fixes the level without changing the ranking: every probability is raised to the
power 1/T and renormalised. T < 1 sharpens, T > 1 softens. One temperature per question.

  python3 calibrate.py [--base-url ...] [--model ...]

Writes calibration.json next to this script; triage.py applies it automatically. The fit uses
all labelled alerts, so the numbers it prints are in-sample. It also prints a two-fold
cross-check (fit on half, measure on the other half) so you can see whether the correction
generalises. The real test is live alerts the model has never seen.
"""

import argparse
import json
import math
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import triage  # noqa: E402

HERE = Path(__file__).resolve().parent
TEMPS = [round(0.15 * 1.06 ** i, 3) for i in range(70)]  # 0.15 … ~8.5


def temper(probs, T):
    z = {k: max(p, 1e-9) ** (1.0 / T) for k, p in probs.items()}
    s = sum(z.values())
    return {k: v / s for k, v in z.items()}


def nll(rows, T):
    """Mean negative log-likelihood of the true label after tempering."""
    return -sum(math.log(max(temper(p, T)[y], 1e-9)) for p, y in rows) / len(rows)


def ece(rows, T=1.0, bins=5):
    pairs = []
    for p, y in rows:
        q = temper(p, T)
        top = max(q, key=q.get)
        pairs.append((q[top], top == y))
    total, n = 0.0, len(pairs)
    for i in range(bins):
        lo, hi = i / bins, (i + 1) / bins + (1e-9 if i == bins - 1 else 0)
        b = [(c, ok) for c, ok in pairs if lo <= c < hi]
        if b:
            total += len(b) / n * abs(sum(ok for _, ok in b) / len(b) - sum(c for c, _ in b) / len(b))
    return total


def fit(rows):
    return min(TEMPS, key=lambda T: nll(rows, T))


def truth_label(alert, q):
    t = alert["truth"]
    if q in ("actionable",):
        return "yes" if t["actionable"] else "no"
    return t.get(q)


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--base-url", default=None)
    ap.add_argument("--model", default=None)
    ap.add_argument("--file", default="alerts.json")
    args = ap.parse_args()
    if args.base_url:
        triage.API = args.base_url.rstrip("/")
    triage.PRICE_PER_MTOK = 0.0
    triage.CALIBRATION = {}  # fit on raw probabilities

    alerts = [a for a in json.loads((HERE / args.file).read_text()) if a.get("truth")]
    print(f"Asking {triage.API} about {len(alerts)} labelled alerts…", flush=True)
    raw = []
    for a in alerts:
        answers, *_ = triage.ask_model(triage.render(a), args.model)
        raw.append(answers)

    result = {}
    print()
    for q in ("component", "cause", "actionable"):
        rows = [(ans[q]["probabilities"], truth_label(a, q)) for ans, a in zip(raw, alerts)
                if "probabilities" in ans[q] and truth_label(a, q) in ans[q]["probabilities"]]
        if len(rows) < 6:
            print(f"{q:<11} not enough labelled rows, skipped")
            continue
        T = fit(rows)
        result[q] = T
        # two-fold cross-check: fit on even rows, score odd rows, and the reverse
        a_, b_ = rows[0::2], rows[1::2]
        cv = (ece(b_, fit(a_)) * len(b_) + ece(a_, fit(b_)) * len(a_)) / len(rows)
        print(f"{q:<11} T = {T:<5}  ECE before {ece(rows):.3f} → after {ece(rows, T):.3f} (in-sample) · {cv:.3f} (two-fold)"
              f"  · mean top-probability {sum(max(p.values()) for p, _ in rows) / len(rows):.2f} → "
              f"{sum(max(temper(p, T).values()) for p, _ in rows) / len(rows):.2f}")

    out = HERE / "calibration.json"
    out.write_text(json.dumps({"model": args.model or triage.API, "temperatures": result,
                               "fitted_on": f"{len(alerts)} alerts from {args.file}"}, indent=2) + "\n")
    print(f"\nwrote {out.name}. triage.py now applies these temperatures; delete the file or pass --no-calibration to compare.")


if __name__ == "__main__":
    main()
