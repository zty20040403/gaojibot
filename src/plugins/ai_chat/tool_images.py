"""A host tool may attach images or videos to the next model input, without sending them."""
from dataclasses import dataclass

from .prompt_context import quote_evidence


def _media_part(url: str) -> dict[str, object]:
    if url.startswith('data:video/'):
        return {'type': 'video_url', 'video_url': {'url': url}}
    return {'type': 'image_url', 'image_url': {'url': url}}


@dataclass(frozen=True)
class ToolImage:
    text: str
    data_url: str = ''
    media_urls: tuple[str, ...] = ()

    def __str__(self):
        return self.text

    def parts(self):
        urls = (*( (self.data_url,) if self.data_url else () ), *self.media_urls)
        kind = 'video' if any(u.startswith('data:video/') for u in urls) else 'image'
        opening, boundary, closing = quote_evidence(self.text, kind).partition('\n</untrusted')
        return [
            {'type': 'text', 'text': opening},
            *(_media_part(u) for u in urls),
            {'type': 'text', 'text': boundary + closing},
        ]
