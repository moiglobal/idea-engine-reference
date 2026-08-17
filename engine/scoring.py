"""Rank the survivors.

Each metric is turned into a percentile (0 to 1) across the day's universe.
"Lower is better" metrics are inverted so that, after ranking, higher always
means better. A factor's score is the average of its metrics' percentiles. The
composite is the weighted sum of factor scores on a 0 to 100 scale.

A missing metric is treated as neutral (0.5), so a company is neither rewarded
nor punished for data the provider did not supply.

This module is pure: give it a list of metric dicts and a scoring config, and
it returns a ranked list. No network, no files.
"""

from __future__ import annotations

import bisect
from typing import List

from .metrics import METRIC_DIRECTION

NEUTRAL = 0.5


def _percentiles_for_metric(values: List, direction: str) -> List:
    """Return a percentile in [0,1] for each value (None stays None)."""
    present = [v for v in values if v is not None]
    n = len(present)
    if n == 0:
        return [None] * len(values)
    ordered = sorted(present)
    out = []
    for v in values:
        if v is None:
            out.append(None)
            continue
        less = bisect.bisect_left(ordered, v)
        eq = bisect.bisect_right(ordered, v) - less
        pct = (less + 0.5 * eq) / n
        if direction == "lower":
            pct = 1.0 - pct
        out.append(pct)
    return out


def score_universe(companies: List[dict], scoring_cfg: dict) -> List[dict]:
    """companies: list of dicts from metrics.compute_metrics (sanity_ok only).

    Returns the same companies, each augmented with composite, factor scores,
    and per-metric percentiles, sorted by composite descending.
    """
    factors = scoring_cfg.get("factors", {})
    used_metrics = sorted({mm for f in factors.values() for mm in f.get("metrics", [])})

    # Percentile-rank every metric across the whole universe.
    pct_by_metric = {}
    for metric in used_metrics:
        direction = METRIC_DIRECTION.get(metric, "higher")
        col = [c["metrics"].get(metric) for c in companies]
        pct_by_metric[metric] = _percentiles_for_metric(col, direction)

    results = []
    for idx, c in enumerate(companies):
        percentiles = {}
        factor_scores = {}
        composite = 0.0
        for fname, fcfg in factors.items():
            weight = float(fcfg.get("weight", 0))
            metric_names = fcfg.get("metrics", [])
            vals = []
            for metric in metric_names:
                p = pct_by_metric[metric][idx]
                percentiles[metric] = p
                vals.append(p if p is not None else NEUTRAL)
            fscore = sum(vals) / len(vals) if vals else NEUTRAL
            factor_scores[fname] = {
                "score": fscore,
                "weight": weight,
                "contribution": weight * fscore,
            }
            composite += weight * fscore
        enriched = dict(c)
        enriched["composite"] = composite
        enriched["factors"] = factor_scores
        enriched["percentiles"] = percentiles
        results.append(enriched)

    results.sort(key=lambda r: r["composite"], reverse=True)
    for rank, r in enumerate(results, start=1):
        r["rank"] = rank
    return results


def weakest_factors(company: dict, k: int = 3) -> List:
    """The k lowest-scoring factors, for the memo's 'where it falls short' section."""
    items = [(name, fc["score"]) for name, fc in company.get("factors", {}).items()]
    items.sort(key=lambda x: x[1])
    return items[:k]


def factor_table(company: dict) -> List:
    """All factors as (name, score 0-100, weight) for display, best first."""
    rows = []
    for name, fc in company.get("factors", {}).items():
        rows.append((name, round(fc["score"] * 100, 1), fc["weight"]))
    rows.sort(key=lambda x: x[1], reverse=True)
    return rows
