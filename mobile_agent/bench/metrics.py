"""Statistics for the report. Pure functions, standard library only."""

import math
import random
import statistics


def wilson(successes, n, z=1.959963984540054):
    """Wilson score interval for a binomial proportion. (nan, nan) for n == 0."""
    if n <= 0:
        return float("nan"), float("nan")
    p = successes / n
    denominator = 1 + z * z / n
    centre = (p + z * z / (2 * n)) / denominator
    half = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / denominator
    return max(0.0, centre - half), min(1.0, centre + half)


def percentile(values, q):
    """Linear-interpolated percentile (q in 0..100); None for no data."""
    values = sorted(v for v in values if v is not None)
    if not values:
        return None
    if len(values) == 1:
        return values[0]
    rank = (len(values) - 1) * q / 100
    low, high = math.floor(rank), math.ceil(rank)
    return values[low] + (values[high] - values[low]) * (rank - low)


def bootstrap_ci(values, statistic=statistics.fmean, resamples=10000, seed=0, alpha=.05):
    """Percentile bootstrap CI of ``statistic`` over ``values`` (resampling units, e.g. tasks)."""
    values = [v for v in values if v is not None]
    if not values:
        return None, None, None
    rng = random.Random(seed)
    stats = sorted(statistic([rng.choice(values) for _ in values]) for _ in range(resamples))
    low = stats[int(alpha / 2 * resamples)]
    high = stats[min(resamples - 1, int((1 - alpha / 2) * resamples))]
    return statistic(values), low, high


def pass_hat_k(outcomes, k):
    """tau-bench's pass^k: probability that k i.i.d. attempts of a task ALL pass, averaged over tasks.

    ``outcomes`` maps task -> list of booleans. Unbiased per task: C(c, k) / C(n, k).
    """
    scores = []
    for results in outcomes.values():
        n, c = len(results), sum(results)
        if n >= k:
            scores.append(math.comb(c, k) / math.comb(n, k))
    return statistics.fmean(scores) if scores else None


def flaky_tasks(outcomes):
    """Tasks whose repeats disagree (some pass, some fail)."""
    return sorted(task for task, results in outcomes.items() if 0 < sum(results) < len(results))


def sign_test(wins, losses):
    """Two-sided exact sign test p-value (ties excluded)."""
    n = wins + losses
    if n == 0:
        return 1.0
    k = min(wins, losses)
    tail = sum(math.comb(n, i) for i in range(k + 1)) / 2 ** n
    return min(1.0, 2 * tail)
