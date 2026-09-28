"""Pure unit tests (no database needed):  pip install -r requirements-dev.txt && pytest"""
import pytest

from app.schemas import ComplaintIn
from app.services.scoring import SYNTHETIC_PLACEHOLDER, get_indicator, normalize, priority, urgency_score


def test_normalize_range():
    assert normalize([1, 2, 3]) == [0.0, 50.0, 100.0]


def test_normalize_equal_values_give_50():
    assert normalize([4, 4]) == [50.0, 50.0]
    assert normalize([7]) == [50.0]          # a single bucket no longer pinned to 0/100
    assert normalize([]) == []


def test_urgency_score_scale():
    assert urgency_score(10) == 100.0        # severity 5 x high(2)
    assert urgency_score(4.5) == 45.0        # severity 3 x medium(1.5)
    assert urgency_score(99) == 100.0        # capped


def test_priority_weights_hand_checked():
    # 947158 / water on the test data: demand 50, infra 82.83, pop_norm 100, urgency 90, funding 50 -> 72.2
    assert round(priority(50, 82.8256, 100, 90, 50), 1) == 72.2
    # all components 100 -> 100 (weights sum to 1)
    assert round(priority(100, 100, 100, 100, 100), 6) == 100


def test_indicator_fallback_order():
    village = {("v1", "water", "infrastructure_gap"): (10.0, False)}
    district = {(("Rajasthan", "Barmer"), "water", "infrastructure_gap"): (82.8, False)}
    assert get_indicator(village, district, "v1", "Rajasthan", "Barmer", "water", "infrastructure_gap")[2] == "village"
    assert get_indicator({}, district, "v1", "Rajasthan", "Barmer", "water", "infrastructure_gap") == (82.8, False, "district")
    assert get_indicator({}, {}, "v1", "Rajasthan", "Barmer", "water", "funding_gap") == (
        SYNTHETIC_PLACEHOLDER, True, "placeholder")


def test_schema_accepts_numeric_pincode_and_normalises_urgency():
    c = ComplaintIn(input_type="text", text="x", category="water", severity=4, urgency=" HIGH ",
                    location={"method": "pincode", "pincode": 342007})
    assert c.location.pincode == "342007" and c.urgency == "high"


def test_schema_rejects_bad_values():
    with pytest.raises(Exception):
        ComplaintIn(input_type="text", text="x", category="power", location={"method": "manual", "lgd_code": "1"})
    with pytest.raises(Exception):
        ComplaintIn(input_type="text", text="x", category="water", severity=6, urgency="high",
                    location={"method": "manual", "lgd_code": "1"})
