"""A host tool may attach an image to the next model input, without sending it."""
from dataclasses import dataclass


@dataclass(frozen=True)
class ToolImage:
    text: str
    data_url: str

    def __str__(self):
        return self.text
