"""Junior growth and maturity calculations.

Pure functions, no AWS dependencies, so they can be unit-tested and reused by
both the measurement Lambda and PT Studio.

Units: centimetres (heights, lengths), kilograms (weight), decimal years (age).

Equations
---------
Moore et al. (2015), "Enhancing a somatic maturity prediction model",
Med Sci Sports Exerc 47(8):1755-64.
    Boys  (sitting height): -8.128741 + 0.0070346 * (age * sitting_height)
    Boys  (height only):    -7.999994 + 0.0036124 * (age * height)
    Girls (height):         -7.709133 + 0.0042232 * (age * height)

Mirwald et al. (2002), "An assessment of maturity from anthropometric
measurements", Med Sci Sports Exerc 34(4):689-94.
    Boys:  -9.236 + 0.0002708*(LL*SH) - 0.001663*(age*LL)
           + 0.007216*(age*SH) + 0.02292*(weight/height*100)
    Girls: -9.376 + 0.0001882*(LL*SH) + 0.0022*(age*LL)
           + 0.005841*(age*SH) - 0.002658*(age*weight)
           + 0.07693*(weight/height*100)
    where LL (leg length) = height - sitting height.

Maturity offset = years from peak height velocity (PHV): negative before,
positive after. Both equations carry roughly +/-1 year of error and are least
accurate in early and late maturers, so treat the result as a guide.

Khamis-Roche % predicted adult height is not implemented yet: it needs the
published coefficient tables, which must be transcribed from the original paper
(Khamis & Roche, 1994, Pediatrics 94:504-7) and checked before use.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from typing import Literal, Optional, Sequence

Sex = Literal["male", "female"]

ALGORITHM_VERSION = "maturity-v1-moore2015-mirwald2002"

# Thresholds from Sullivan et al. (2024) Delphi consensus, PLoS ONE 19(11).
RAPID_GROWTH_CM_PER_MONTH = 0.6
CIRCA_PHV_PAH_LOW = 88.0
CIRCA_PHV_PAH_HIGH = 95.0
# Risk window is during and about 12 months after PHV.
CIRCA_PHV_OFFSET_LOW = -1.0
CIRCA_PHV_OFFSET_HIGH = 1.0

# Minimum gap between measurements before a growth rate is trusted. Stadiometer
# error is ~0.3 cm, so rates over very short gaps are mostly noise.
MIN_VELOCITY_GAP_DAYS = 28
DAYS_PER_MONTH = 30.4375


def decimal_age(date_of_birth: date, on: date) -> float:
    """Age in decimal years on a given date."""
    if on < date_of_birth:
        raise ValueError("measurement date is before date of birth")
    return (on - date_of_birth).days / 365.25


def is_junior(date_of_birth: date, on: date) -> bool:
    """Under 18 on the given date. Derived, never stored."""
    return decimal_age(date_of_birth, on) < 18.0


def moore_offset(sex: Sex, age: float, height_cm: float,
                 sitting_height_cm: Optional[float] = None) -> float:
    """Moore et al. (2015) maturity offset in years."""
    if sex == "female":
        return -7.709133 + 0.0042232 * (age * height_cm)
    if sitting_height_cm is not None:
        return -8.128741 + 0.0070346 * (age * sitting_height_cm)
    return -7.999994 + 0.0036124 * (age * height_cm)


def mirwald_offset(sex: Sex, age: float, height_cm: float,
                   sitting_height_cm: float, weight_kg: float) -> float:
    """Mirwald et al. (2002) maturity offset in years."""
    sh = sitting_height_cm
    ll = height_cm - sitting_height_cm
    wt_ht = weight_kg / height_cm * 100
    if sex == "male":
        return (-9.236
                + 0.0002708 * (ll * sh)
                - 0.001663 * (age * ll)
                + 0.007216 * (age * sh)
                + 0.02292 * wt_ht)
    return (-9.376
            + 0.0001882 * (ll * sh)
            + 0.0022 * (age * ll)
            + 0.005841 * (age * sh)
            - 0.002658 * (age * weight_kg)
            + 0.07693 * wt_ht)


def mid_parent_height(parent_a_cm: Optional[float],
                      parent_b_cm: Optional[float]) -> Optional[float]:
    """Average of both biological parents' heights.

    Only this value is stored; the two raw heights are discarded. Returns
    None if either is unknown, in which case %PAH is skipped.
    """
    if parent_a_cm is None or parent_b_cm is None:
        return None
    for h in (parent_a_cm, parent_b_cm):
        if not 120 <= h <= 230:
            raise ValueError(f"parent height {h} cm is outside a plausible range")
    return round((parent_a_cm + parent_b_cm) / 2, 1)


@dataclass(frozen=True)
class Measurement:
    measured_on: date
    height_cm: float
    sitting_height_cm: Optional[float] = None
    weight_kg: Optional[float] = None


def growth_velocity(current: Measurement,
                    history: Sequence[Measurement]) -> Optional[float]:
    """Height gain in cm per month versus the most recent earlier measurement
    at least MIN_VELOCITY_GAP_DAYS before this one. None if there isn't one."""
    earlier = [m for m in history
               if (current.measured_on - m.measured_on).days >= MIN_VELOCITY_GAP_DAYS]
    if not earlier:
        return None
    prev = max(earlier, key=lambda m: m.measured_on)
    months = (current.measured_on - prev.measured_on).days / DAYS_PER_MONTH
    return round((current.height_cm - prev.height_cm) / months, 2)


Stage = Literal["pre_phv", "circa_phv", "post_phv"]


@dataclass
class MaturityResult:
    age: float
    moore_offset: float
    mirwald_offset: Optional[float]
    predicted_aphv: float
    growth_cm_per_month: Optional[float]
    percent_pah: Optional[float]
    stage: Stage
    flags: list[str] = field(default_factory=list)
    method: str = ""
    algorithm_version: str = ALGORITHM_VERSION


def stage_from(offset: float, percent_pah: Optional[float]) -> Stage:
    if percent_pah is not None:
        if percent_pah < CIRCA_PHV_PAH_LOW:
            return "pre_phv"
        if percent_pah <= CIRCA_PHV_PAH_HIGH:
            return "circa_phv"
        return "post_phv"
    if offset < CIRCA_PHV_OFFSET_LOW:
        return "pre_phv"
    if offset <= CIRCA_PHV_OFFSET_HIGH:
        return "circa_phv"
    return "post_phv"


def assess(sex: Sex, date_of_birth: date, current: Measurement,
           history: Sequence[Measurement] = (),
           percent_pah: Optional[float] = None) -> MaturityResult:
    """Full assessment for one measurement.

    percent_pah is accepted for when Khamis-Roche is added; until then pass None
    and staging uses the Moore maturity offset.
    """
    age = decimal_age(date_of_birth, current.measured_on)
    moore = moore_offset(sex, age, current.height_cm, current.sitting_height_cm)

    mirwald = None
    if current.sitting_height_cm is not None and current.weight_kg is not None:
        mirwald = mirwald_offset(sex, age, current.height_cm,
                                 current.sitting_height_cm, current.weight_kg)

    velocity = growth_velocity(current, history)
    stage = stage_from(moore, percent_pah)
    flags: list[str] = []

    if velocity is not None and velocity > RAPID_GROWTH_CM_PER_MONTH:
        flags.append("rapid_growth")
        # Fast growth is itself the warning sign: treat as circa-PHV.
        if stage == "pre_phv":
            stage = "circa_phv"

    if sex == "male" and not 11 <= age <= 16:
        flags.append("offset_unreliable_for_age")
    if sex == "female" and not 9 <= age <= 15:
        flags.append("offset_unreliable_for_age")
    if velocity is None:
        flags.append("no_velocity_yet")

    method = "khamis_roche_pah" if percent_pah is not None else "moore_offset"

    return MaturityResult(
        age=round(age, 2),
        moore_offset=round(moore, 2),
        mirwald_offset=None if mirwald is None else round(mirwald, 2),
        predicted_aphv=round(age - moore, 2),
        growth_cm_per_month=velocity,
        percent_pah=percent_pah,
        stage=stage,
        flags=flags,
        method=method,
    )
