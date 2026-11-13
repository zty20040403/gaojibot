"""Provider receipts after protocol normalization, never VM estimates/reservations."""
from dataclasses import dataclass
from decimal import Decimal

from .model_pricing import CostQuote

_MISSING = object()


def field(value,name,default=None):
    return value.get(name,default) if isinstance(value,dict) else getattr(value,name,default)


def _count(value):
    return value if type(value) is int and 0<=value<=2**63-1 else None


@dataclass(frozen=True)
class TokenUsage:
    input_tokens: int | None
    output_tokens: int | None
    cached_tokens: int | None
    cache_write_tokens: int | None

    def __post_init__(self):
        values = (self.input_tokens,self.output_tokens,self.cached_tokens,self.cache_write_tokens)
        if any(value is not None and _count(value) is None for value in values):
            raise ValueError('invalid_provider_token_usage')
        if self.input_tokens is not None and sum(value or 0 for value in values[2:])>self.input_tokens:
            raise ValueError('inconsistent_provider_token_usage')

    @classmethod
    def read(cls,value):
        # Gateway Anthropic adapters normalize total input to prompt_tokens,
        # including cache reads/writes. Responses API uses input_tokens instead.
        incoming = _count(field(value,'prompt_tokens',field(value,'input_tokens')))
        outgoing = _count(field(value,'completion_tokens',field(value,'output_tokens')))
        details = field(value,'prompt_tokens_details',field(value,'input_tokens_details'))
        cached = field(details,'cached_tokens',field(value,'prompt_cache_hit_tokens',
            field(value,'cache_read_input_tokens',_MISSING)))
        if incoming==0 and cached is _MISSING:cached = 0
        cached = _count(cached)
        writes = _count(field(value,'cache_creation_input_tokens',0))
        # Contradictory detail cannot be used to under/overcharge the total.
        if incoming is not None and (cached or 0)+(writes or 0)>incoming:
            cached = writes = None
        return cls(incoming,outgoing,cached,writes)

    @classmethod
    def anthropic(cls,value):
        incoming = _count(field(value,'input_tokens'))
        outgoing = _count(field(value,'output_tokens'))
        cached = _count(field(value,'cache_read_input_tokens',0))
        writes = _count(field(value,'cache_creation_input_tokens',0))
        total = _count(incoming+cached+writes) if all(count is not None for count in (incoming,cached,writes)) else None
        return cls(total,outgoing,cached,writes)

    @property
    def known(self):
        return self.input_tokens is not None and self.output_tokens is not None

    def quote(self,pricing):
        if pricing is None:return CostQuote(Decimal(0),False)
        if self.known and self.cached_tokens is not None and self.cache_write_tokens is not None:
            return pricing.quote(input_tokens=self.input_tokens,output_tokens=self.output_tokens,
                cached_tokens=self.cached_tokens,cache_write_tokens=self.cache_write_tokens)
        # With incomplete cache details only explicitly reported cached input
        # and output are priced. Never charge all input at a guessed rate.
        cached = self.cached_tokens or 0
        quote = pricing.quote(input_tokens=cached,cached_tokens=cached,output_tokens=self.output_tokens or 0)
        return CostQuote(quote.usd,False)
