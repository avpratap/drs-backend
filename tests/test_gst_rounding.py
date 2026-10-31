"""GST rounding: CGST == SGST on every intra-state line (each is rate/2 on the same
taxable value), lines still sum to what was charged, and an order-level discount is
apportioned to every line to within a paisa (not dumped on the last line)."""
import pytest

from app.services.invoicing import _apportion_paise, _inclusive_split


@pytest.mark.parametrize("charged", [0.01, 0.03, 1.07, 10.0, 27.0, 99.99, 135.0, 500.0, 1234.57])
@pytest.mark.parametrize("rate", [0, 5, 12, 18, 28])
def test_cgst_equals_sgst_and_sums_to_charged(charged, rate):
    taxable, c, s, i = _inclusive_split(charged, rate, interstate=False)
    assert c == s and i == 0.0
    assert round(taxable + c + s, 2) == charged
    # Each half is rate/2 of the taxable value, to the paisa.
    assert abs(c - taxable * rate / 200) <= 0.011


def test_odd_paisa_tax_no_longer_splits_unevenly():
    # Old behaviour: total tax 0.03 -> cgst 0.02 / sgst 0.01.
    _taxable, c, s, _ = _inclusive_split(0.65, 5, interstate=False)
    assert c == s


def test_interstate_unchanged():
    taxable, c, s, i = _inclusive_split(118.0, 18, interstate=True)
    assert (taxable, c, s, i) == (100.0, 0.0, 0.0, 18.0)


def test_discount_apportioned_within_a_paisa_of_each_lines_share():
    lines = [100.0, 100.0, 100.0]  # 3 equal lines, ₹1 discount -> 299.00 to split
    alloc = _apportion_paise(lines, 29_900)
    assert sum(alloc) == 299.0
    assert round(max(alloc) - min(alloc), 2) <= 0.01  # old: last line took the remainder
    exact = [w * 299.0 / 300.0 for w in lines]
    assert all(round(abs(a - e), 4) <= 0.01 for a, e in zip(alloc, exact, strict=True))


def test_apportion_edge_cases():
    assert _apportion_paise([], 0) == []
    assert _apportion_paise([0.0, 0.0], 0) == [0.0, 0.0]
    assert sum(_apportion_paise([33.33, 66.67, 0.0], 9_000)) == 90.0
    assert _apportion_paise([10.0], 1_000) == [10.0]
