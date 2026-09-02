"""Fee, GST and reserve verification (FR-1.4).

The point of this module is independence: it recomputes what the deduction
*should* have been from the merchant's contracted rate card, without reference
to what Razorpay actually deducted. Only after computing the expected figure
does it compare. A verifier that read the actual value first would be able to
rationalise any deduction, which is precisely the settlement-opacity problem
the PRD sets out to close.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import ROUND_HALF_UP, Decimal

from ..config import Rules
from ..models import ReasonCode


@dataclass(frozen=True)
class FeeExpectation:
    """What the rate card says should have been deducted, in paise."""

    fee_paise: int
    gst_paise: int
    reserve_paise: int

    @property
    def total_paise(self) -> int:
        return self.fee_paise + self.gst_paise + self.reserve_paise


@dataclass(frozen=True)
class FeeDiscrepancy:
    """One component of the deduction that did not match expectation."""

    reason_code: ReasonCode
    expected_paise: int
    actual_paise: int

    @property
    def delta_paise(self) -> int:
        """Positive means the merchant was charged more than expected."""
        return self.actual_paise - self.expected_paise


def _round_paise(value: Decimal) -> int:
    """Round a paise amount to a whole paise, half away from zero.

    Decimal with an explicit rounding mode rather than Python's ``round``:
    ``round`` uses banker's rounding, which disagrees with the half-up
    convention payment processors use and would produce single-paise
    disagreements on exact-half amounts.
    """
    return int(value.quantize(Decimal("1"), rounding=ROUND_HALF_UP))


def compute_expected_fees(
    amount_paise: int,
    method: str,
    rules: Rules,
    merchant: str = "default",
) -> FeeExpectation:
    """Recompute the expected deduction for one transaction (FR-1.4).

    Expected fee = amount x MDR rate; expected GST = fee x 18%. GST is computed
    on the rounded fee, not the raw product, because that is the order a
    processor bills in - GST is levied on the fee actually charged.
    """
    amount = Decimal(amount_paise)

    fee_rate = Decimal(str(rules.fee_rate_for(method, merchant)))
    fee_paise = _round_paise(amount * fee_rate)

    gst_rate = Decimal(str(rules.gst_rate))
    gst_paise = _round_paise(Decimal(fee_paise) * gst_rate)

    reserve_rate = Decimal(str(rules.reserve_rate_for(merchant)))
    reserve_paise = _round_paise(amount * reserve_rate)

    return FeeExpectation(
        fee_paise=fee_paise, gst_paise=gst_paise, reserve_paise=reserve_paise
    )


def verify_fees(
    expected: FeeExpectation,
    actual_fee_paise: int,
    actual_gst_paise: int,
    actual_reserve_paise: int,
    rules: Rules,
) -> list[FeeDiscrepancy]:
    """Compare expected against actual, component by component.

    Components are checked separately rather than as one total so the reason
    code names the actual problem: a wrong MDR rate and a wrong GST rate are
    different defects with different fixes, and a combined check could even let
    two opposing errors cancel out into a false pass.
    """
    tolerance = rules.fee_tolerance_paise
    checks = (
        (ReasonCode.FEE_MISMATCH, expected.fee_paise, actual_fee_paise),
        (ReasonCode.GST_MISMATCH, expected.gst_paise, actual_gst_paise),
        (
            ReasonCode.RESERVE_MISMATCH,
            expected.reserve_paise,
            actual_reserve_paise,
        ),
    )

    return [
        FeeDiscrepancy(
            reason_code=reason, expected_paise=exp, actual_paise=act
        )
        for reason, exp, act in checks
        if abs(act - exp) > tolerance
    ]
