"""Heart rate zone calculation.

Uses the Karvonen (heart rate reserve) method when a resting HR is on file,
since it's more accurate than max-HR-only, particularly for well-trained
clients. Falls back to percent-of-max when resting HR is unknown.

Zones (standard 5-zone model, expressed as % of HRR or % of max):
  Z1 Recovery   50-60%
  Z2 Aerobic    60-70%
  Z3 Tempo      70-80%
  Z4 Threshold  80-90%
  Z5 Max        90-100%
"""

ZONE_BOUNDS = [0.50, 0.60, 0.70, 0.80, 0.90, 1.00]
ZONE_NAMES = ["Z1 Recovery", "Z2 Aerobic", "Z3 Tempo", "Z4 Threshold", "Z5 Max"]
ZONE_COLORS = ["#4a90d9", "#3fbfad", "#8bcf7a", "#f2a93b", "#e5484d"]


def estimate_max_hr(age: int | None) -> int:
    """Tanaka formula: 208 - 0.7 * age. Falls back to a generic 190 if age unknown."""
    if age is None:
        return 190
    return round(208 - 0.7 * age)


def zone_thresholds(age: int | None, resting_hr: int | None) -> list[int]:
    """Returns the 6 bpm boundaries [Z1 low, ..., Z5 high]."""
    hr_max = estimate_max_hr(age)
    if resting_hr:
        hrr = hr_max - resting_hr
        return [round(resting_hr + b * hrr) for b in ZONE_BOUNDS]
    return [round(b * hr_max) for b in ZONE_BOUNDS]


def zone_for_hr(hr: int, thresholds: list[int]) -> int:
    """Returns zone index 0-4 (Z1-Z5), clamped at the edges."""
    for i in range(5):
        if hr <= thresholds[i + 1]:
            return i
    return 4
