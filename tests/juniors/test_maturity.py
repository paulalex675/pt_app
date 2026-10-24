from datetime import date

import pytest

from juniors.maturity import (
    Measurement,
    assess,
    decimal_age,
    growth_velocity,
    is_junior,
    mid_parent_height,
    mirwald_offset,
    moore_offset,
    stage_from,
)


def test_is_junior_flips_on_18th_birthday():
    dob = date(2008, 10, 22)
    assert is_junior(dob, date(2026, 10, 21))
    assert not is_junior(dob, date(2026, 10, 23))


def test_moore_boy_sitting_height_hand_calc():
    # -8.128741 + 0.0070346 * (13.5 * 85) = -0.0563...
    assert moore_offset("male", 13.5, 165, 85) == pytest.approx(-0.0563, abs=1e-3)


def test_moore_girl_hand_calc():
    # -7.709133 + 0.0042232 * (12 * 155) = 0.1460...
    assert moore_offset("female", 12, 155) == pytest.approx(0.1460, abs=1e-3)


def test_mirwald_boy_hand_calc():
    age, ht, sh, wt = 13.5, 165.0, 85.0, 52.0
    ll = ht - sh
    expected = (-9.236 + 0.0002708 * ll * sh - 0.001663 * age * ll
                + 0.007216 * age * sh + 0.02292 * wt / ht * 100)
    assert mirwald_offset("male", age, ht, sh, wt) == pytest.approx(expected)


def test_mid_parent_height_only_when_both_known():
    assert mid_parent_height(180, 165) == 172.5
    assert mid_parent_height(180, None) is None
    with pytest.raises(ValueError):
        mid_parent_height(18.0, 165)  # metres typed instead of cm


def test_velocity_ignores_too_recent_measurements():
    cur = Measurement(date(2026, 10, 1), 160.0)
    history = [Measurement(date(2026, 7, 1), 158.0),
               Measurement(date(2026, 9, 20), 159.9)]  # only 11 days earlier
    v = growth_velocity(cur, history)
    # 2.0 cm over 92 days (~3.02 months)
    assert v == pytest.approx(0.66, abs=0.01)


def test_velocity_none_without_history():
    assert growth_velocity(Measurement(date(2026, 10, 1), 160.0), []) is None


@pytest.mark.parametrize("offset,pah,stage", [
    (-2.0, None, "pre_phv"),
    (-0.5, None, "circa_phv"),
    (1.0, None, "circa_phv"),
    (1.5, None, "post_phv"),
    (0.0, 85.0, "pre_phv"),   # %PAH overrides offset when available
    (0.0, 90.0, "circa_phv"),
    (0.0, 97.0, "post_phv"),
])
def test_stage_rules(offset, pah, stage):
    assert stage_from(offset, pah) == stage


def test_rapid_growth_promotes_pre_phv_to_circa():
    dob = date(2015, 1, 1)
    cur = Measurement(date(2026, 10, 1), 145.0, 74.0, 36.0)
    history = [Measurement(date(2026, 7, 1), 142.5)]  # ~0.83 cm/month
    r = assess("male", dob, cur, history)
    assert r.moore_offset < -1  # offset alone says pre-PHV
    assert "rapid_growth" in r.flags
    assert r.stage == "circa_phv"


def test_decimal_age_rejects_bad_dates():
    with pytest.raises(ValueError):
        decimal_age(date(2015, 1, 1), date(2014, 1, 1))
