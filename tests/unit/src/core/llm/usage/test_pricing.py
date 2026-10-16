from decimal import Decimal

from pydantic import ValidationError
import pytest

from src.core.llm.types import LLMUsage
from src.core.llm.usage.pricing import COST_QUANTUM, PRICES, ModelPrice, PricingTable
from tests.helpers.logs import captured_logs

MILLION = 1_000_000


def test_every_kind_of_token_is_billed_at_its_own_rate() -> None:
    """Cache reads cost a tenth of input and cache writes a quarter more; a
    table that billed them as input would misreport a cached prompt."""
    usage = LLMUsage(
        input_tokens=MILLION,
        output_tokens=MILLION,
        cache_read_tokens=MILLION,
        cache_write_tokens=MILLION,
    )

    # 2.00 + 10.00 + 0.20 + 2.50
    assert PricingTable().cost("claude-sonnet-5-5", usage) == Decimal("14.70000000")


def test_the_smallest_charge_is_not_rounded_away() -> None:
    """Thousands of tiny calls add up; rounding each to zero would hide them
    from /stats."""
    usage = LLMUsage(input_tokens=0, output_tokens=0, cache_read_tokens=1)

    assert PricingTable().cost("claude-haiku-5-5", usage) == COST_QUANTUM


def test_a_half_quantum_rounds_up() -> None:
    """One cache-write token of claude-haiku-5-5 is $0.000000125, exactly
    between two quanta. Half-up is the usual money rounding; the default
    half-even would round it down and make ties inconsistent."""
    usage = LLMUsage(input_tokens=0, output_tokens=0, cache_write_tokens=1)

    assert PricingTable().cost("claude-haiku-5-5", usage) == Decimal("0.00000013")


def test_a_cost_has_the_scale_of_the_usage_column() -> None:
    """The cost goes into a Numeric(14, 8) column; any other scale would be
    rounded again by the database."""
    cost = PricingTable().cost("claude-haiku-5-5", LLMUsage(123, 45))

    assert cost == Decimal("0.00003480")
    assert cost is not None and cost.as_tuple().exponent == -8


def test_an_override_replaces_a_built_in_price() -> None:
    """A price change at Anthropic must be fixable from .env, without a release."""
    table = PricingTable(
        {
            "claude-sonnet-5-5": ModelPrice(
                input_per_mtok=Decimal("1"),
                output_per_mtok=Decimal("0"),
                cache_write_per_mtok=Decimal("0"),
                cache_read_per_mtok=Decimal("0"),
            )
        }
    )

    assert table.cost("claude-sonnet-5-5", LLMUsage(MILLION, MILLION)) == Decimal(
        "1.00000000"
    )


def test_an_override_adds_a_model_and_keeps_the_others() -> None:
    """A new model must be priceable without losing the built-in ones."""
    table = PricingTable(
        {
            "claude-opus-5-5": ModelPrice(
                input_per_mtok=Decimal("4"),
                output_per_mtok=Decimal("20"),
                cache_write_per_mtok=Decimal("5"),
                cache_read_per_mtok=Decimal("0.40"),
            )
        }
    )

    assert table.cost("claude-opus-5-5", LLMUsage(MILLION, 0)) == Decimal("4.00000000")
    assert table.cost("claude-haiku-5-5", LLMUsage(MILLION, 0)) == Decimal("0.10000000")


def test_an_unknown_model_has_no_cost_and_warns_once() -> None:
    """None, not zero: a zero would read as a free call. One warning per
    model, because a per-call warning would flood the log."""
    table = PricingTable()

    with captured_logs("src.core.llm.usage.pricing") as records:
        first = table.cost("claude-unknown-1", LLMUsage(10, 10))
        second = table.cost("claude-unknown-1", LLMUsage(10, 10))
        other = table.cost("claude-unknown-2", LLMUsage(10, 10))

    assert first is second is other is None
    warnings = [record.getMessage() for record in records]
    assert len(warnings) == 2
    assert "claude-unknown-1" in warnings[0]
    assert "LLM_PRICING_OVERRIDES" in warnings[0]
    assert "claude-unknown-2" in warnings[1]


def test_the_built_in_prices_cannot_be_changed_at_run_time() -> None:
    """One table is shared by the whole process; a write through PRICES
    would change the cost of every later call."""
    with pytest.raises(TypeError):
        PRICES["claude-sonnet-5-5"] = PRICES["claude-haiku-5-5"]  # type: ignore[index]


def test_a_negative_rate_is_refused() -> None:
    """A negative price would subtract from the totals in /stats."""
    with pytest.raises(ValidationError, match="input_per_mtok"):
        ModelPrice(
            input_per_mtok=Decimal("-1"),
            output_per_mtok=Decimal("1"),
            cache_write_per_mtok=Decimal("1"),
            cache_read_per_mtok=Decimal("1"),
        )
