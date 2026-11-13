"""Operator-supplied USD rates, exact arithmetic, and explicit incomplete quotes.

These rates are not fetched from providers. A quote is not a provider invoice;
unknown usage, missing prices and reservations must be handled by the caller.
"""
from collections.abc import Mapping
from dataclasses import dataclass, fields
from decimal import Context, Decimal, InvalidOperation, localcontext
import re

_SCALE = Decimal('0.000001')
_MILLION = Decimal(1_000_000)


def _rate(value):
    if type(value) not in (str,int,float,Decimal):raise ValueError('pricing_rate_must_be_decimal')
    raw = str(value)
    if len(raw)>64 or not re.fullmatch(r'[0-9]+(?:\.[0-9]+)?(?:[eE][+-]?[0-9]+)?',raw):
        raise ValueError('pricing_rate_must_be_decimal')
    try:
        parsed = Decimal(raw)
        with localcontext(Context(prec=40)):
            if not parsed.is_finite() or not 0<=parsed<=_MILLION or parsed!=parsed.quantize(_SCALE):
                raise ValueError('pricing_rate_out_of_range_or_precision')
            return parsed.quantize(_SCALE)
    except InvalidOperation:
        raise ValueError('pricing_rate_out_of_range_or_precision') from None


@dataclass(frozen=True)
class CostQuote:
    usd: Decimal
    complete: bool


@dataclass(frozen=True)
class ModelPricing:
    input_usd_per_million: Decimal
    cached_input_usd_per_million: Decimal
    output_usd_per_million: Decimal

    def __post_init__(self):
        for item in fields(self):
            object.__setattr__(self,item.name,_rate(getattr(self,item.name)))

    @classmethod
    def from_mapping(cls,raw):
        if raw is None:return None
        if not isinstance(raw,Mapping) or set(raw)!={item.name for item in fields(cls)}:
            raise ValueError('pricing_requires_input_cached_input_and_output_usd_per_million')
        return cls(**raw)

    def to_mapping(self):
        return {item.name:format(getattr(self,item.name),'f') for item in fields(self)}

    def quote(self, *, input_tokens, output_tokens, cached_tokens=0, cache_write_tokens=0):
        counts = (input_tokens,output_tokens,cached_tokens,cache_write_tokens)
        if any(type(count) is not int or not 0<=count<=2**63-1 for count in counts):
            raise ValueError('pricing_requires_known_nonnegative_token_counts')
        if cached_tokens+cache_write_tokens>input_tokens:
            raise ValueError('pricing_input_counts_inconsistent')
        # Protocol adapters report total input including cached reads/writes.
        # Cache creation has no declared rate in this three-price contract:
        # omit that bucket and mark a lower bound, never silently price it as
        # ordinary input or mistake an estimate for a known cache-write charge.
        with localcontext(Context(prec=50)):
            usd = ((input_tokens-cached_tokens-cache_write_tokens)*self.input_usd_per_million
                + cached_tokens*self.cached_input_usd_per_million
                + output_tokens*self.output_usd_per_million)/_MILLION
        return CostQuote(usd,cache_write_tokens==0)
