from collections.abc import Mapping
from decimal import ROUND_HALF_UP, Decimal
from types import MappingProxyType

from pydantic import BaseModel, ConfigDict, Field

from loggers import get_logger
from src.core.llm.types import LLMUsage

logger = get_logger(__name__)

_TOKENS_PER_PRICE_UNIT = Decimal(1_000_000)
# Eight places hold the smallest built-in charge exactly: one cache-read token
# of claude-haiku-5-5 is $0.00000001. llm_usage.cost_usd keeps the same scale.
COST_QUANTUM = Decimal("0.00000001")


class ModelPrice(BaseModel):
    """US dollars per million tokens of one model.

    `cache_write_per_mtok` is the five-minute cache write rate, the one a plain
    `cache_control: ephemeral` block is billed at.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    input_per_mtok: Decimal = Field(ge=0)
    output_per_mtok: Decimal = Field(ge=0)
    cache_write_per_mtok: Decimal = Field(ge=0)
    cache_read_per_mtok: Decimal = Field(ge=0)


# Anthropic list prices, checked 2026-10-14. claude-haiku-5-5 has two rate
# cards split at a 100K-token prompt; this is the shorter-prompt card, the one
# a chat bot's requests fall under, and a longer prompt is undercounted.
# OpenRouter reports the cost of each call itself and needs no entry here.
PRICES: Mapping[str, ModelPrice] = MappingProxyType(
    {
        "claude-sonnet-5-5": ModelPrice(
            input_per_mtok=Decimal("2.00"),
            output_per_mtok=Decimal("10.00"),
            cache_write_per_mtok=Decimal("2.50"),
            cache_read_per_mtok=Decimal("0.20"),
        ),
        "claude-haiku-5-5": ModelPrice(
            input_per_mtok=Decimal("0.10"),
            output_per_mtok=Decimal("0.50"),
            cache_write_per_mtok=Decimal("0.125"),
            cache_read_per_mtok=Decimal("0.01"),
        ),
    }
)


class PricingTable:
    """Dollars for the token counts of a provider that does not report a cost.

    Built once per process (APP scope) from PRICES and LLM_PRICING_OVERRIDES;
    an override replaces or adds a whole entry. A model with no price logs one
    warning for the lifetime of the table, not one per call, and its calls
    are recorded with no cost.
    """

    def __init__(self, overrides: Mapping[str, ModelPrice] | None = None) -> None:
        self._prices: dict[str, ModelPrice] = {**PRICES, **(overrides or {})}
        self._unpriced_models: set[str] = set()

    def cost(self, model: str, usage: LLMUsage) -> Decimal | None:
        price = self._prices.get(model)
        if price is None:
            if model not in self._unpriced_models:
                self._unpriced_models.add(model)
                logger.warning(
                    "No price for LLM model %s: its calls are recorded without a "
                    "cost. Add it to LLM_PRICING_OVERRIDES.",
                    model,
                )
            return None
        dollars = (
            usage.input_tokens * price.input_per_mtok
            + usage.output_tokens * price.output_per_mtok
            + usage.cache_write_tokens * price.cache_write_per_mtok
            + usage.cache_read_tokens * price.cache_read_per_mtok
        ) / _TOKENS_PER_PRICE_UNIT
        return dollars.quantize(COST_QUANTUM, rounding=ROUND_HALF_UP)
