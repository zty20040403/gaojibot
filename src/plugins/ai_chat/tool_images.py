"""A host tool may attach an image to the next model input, without sending it."""
from dataclasses import dataclass

from .prompt_context import quote_evidence


@dataclass(frozen=True)
class ToolImage:
    text: str
    data_url: str

    def __str__(self):
        return self.text

    def parts(self):
        opening, boundary, closing = quote_evidence(self.text, 'image').partition('\n</untrusted')
        return [{'type': 'text', 'text': opening},
                {'type': 'image_url', 'image_url': {'url': self.data_url}},
                {'type': 'text', 'text': boundary + closing}]
