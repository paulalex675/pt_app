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


def zone_thresholds(
    age: int | None,
    resting_hr: int | None,
    max_hr: int | None = None,
) -> list[int]:
    """Returns the 6 bpm boundaries [Z1 low, ..., Z5 high]."""
    hr_max = max_hr or estimate_max_hr(age)
    if resting_hr:
        hrr = hr_max - resting_hr
        return [round(resting_hr + b * hrr) for b in ZONE_BOUNDS]
    return [round(b * hr_max) for b in ZONE_BOUNDS]


def zone_for_hr(hr: int, thresholds: list[int]) -> int:
    """Returns zone index 0-4 (Z1-Z5), clamped at the edges.

    Any value below the first threshold is treated as the lowest zone (Z1)
    so a resting HR still maps to a valid zone instead of falling through.
    """
    if not thresholds:
        return 0
    if hr < thresholds[0]:
        return 0
    for i in range(5):
        if hr <= thresholds[i + 1]:
            return i
    return 4


# A chest strap can spike (dry electrodes, movement). A new max HR is only saved
# if it is physiologically plausible AND held for several consecutive readings.
MAX_PLAUSIBLE_HR = 220
NEW_MAX_CONFIRM_READINGS = 3


def track_new_max(streak: dict, hr: int, current_max: int) -> int | None:
    """Tracks consecutive readings above current_max.

    `streak` is a small mutable dict owned by the caller. Returns the confirmed
    new max (the lowest reading in the streak) once enough consecutive plausible
    readings exceed current_max, otherwise None.
    """
    if current_max < hr <= MAX_PLAUSIBLE_HR:
        streak["count"] = streak.get("count", 0) + 1
        streak["floor"] = hr if streak["count"] == 1 else min(streak["floor"], hr)
        if streak["count"] >= NEW_MAX_CONFIRM_READINGS:
            confirmed = streak["floor"]
            streak["count"] = 0
            return confirmed
        return None
    streak["count"] = 0
    return None
