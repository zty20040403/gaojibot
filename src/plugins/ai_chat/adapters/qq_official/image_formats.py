"""Bounded PNG/JPEG artifact contract; neither MIME nor extension is evidence.

This adapter validates container structure, dimensions and the complete PNG
scanline stream. It is not an OCR/image-content safety classifier. GIF, animated
PNG, SVG, WebP and video/audio remain unavailable in this initial contract.
"""
import binascii
from pathlib import PurePath
import struct
import zlib
from src.plugins.ai_chat.agent.artifact_delivery import canonical_artifact_filename


MAX_IMAGE_BYTES = 3_500_000
MAX_IMAGE_PIXELS = 16_000_000
MAX_IMAGE_DECODED_BYTES = 64_000_000
IMAGE_MIMES = frozenset({'image/png', 'image/jpeg'})
ALL_ARTIFACT_MIMES = IMAGE_MIMES | {'application/pdf', 'application/zip'}


class ImageFormatError(ValueError):
    """Fixed safe diagnostic only."""


def file_type_for_mime(mime):
    return 1 if mime.startswith('image/') else 4


def mime_matches_filename(mime, filename):
    suffix = PurePath(filename).suffix.lower()
    return suffix in {'image/png': {'.png'}, 'image/jpeg': {'.jpg', '.jpeg'},
                     'application/pdf': {'.pdf'}, 'application/zip': {'.zip'}}.get(mime, set())


def artifact_filename(filename, *, draft):
    return canonical_artifact_filename(filename, draft=draft)


def _dimensions(width, height):
    if not 1 <= width <= 8192 or not 1 <= height <= 8192 or width * height > MAX_IMAGE_PIXELS:
        raise ImageFormatError('image_dimension_limit')


def _png(data):
    offset, chunks, header, ended, after_idat = 8, 0, None, False, False
    compressed = bytearray()
    while offset < len(data):
        if offset + 12 > len(data) or chunks >= 4096:
            raise ImageFormatError('invalid_png_structure')
        length = int.from_bytes(data[offset:offset + 4], 'big')
        kind = data[offset + 4:offset + 8]
        end = offset + 12 + length
        if end > len(data) or binascii.crc32(data[offset + 4:end - 4]) & 0xffffffff != int.from_bytes(data[end - 4:end], 'big'):
            raise ImageFormatError('invalid_png_checksum')
        value = data[offset + 8:end - 4]
        chunks += 1
        if chunks == 1:
            if kind != b'IHDR' or length != 13:
                raise ImageFormatError('invalid_png_header')
            width, height, depth, color, method, filter_method, interlace = struct.unpack('>IIBBBBB', value)
            _dimensions(width, height)
            if depth != 8 or color not in {0, 2, 4, 6} or method or filter_method or interlace:
                raise ImageFormatError('png_variant_unavailable')
            channels = {0: 1, 2: 3, 4: 2, 6: 4}[color]
            row_size = 1 + width * channels
            expected = height * row_size
            if expected > MAX_IMAGE_DECODED_BYTES:
                raise ImageFormatError('image_decoded_limit')
            header = (height, row_size, expected)
        elif kind == b'IHDR' or kind in {b'acTL', b'fcTL', b'fdAT'}:
            raise ImageFormatError('png_variant_unavailable')
        elif kind == b'IDAT':
            if after_idat:
                raise ImageFormatError('invalid_png_structure')
            compressed.extend(value)
        elif kind == b'IEND':
            if length or end != len(data):
                raise ImageFormatError('invalid_png_structure')
            ended = True
        else:
            if compressed:
                after_idat = True
            # Unknown critical chunks are not safe to ignore. PLTE optional for
            # RGB; its entries are bounded and not used to decode admitted types.
            if kind == b'PLTE':
                if length % 3 or not 3 <= length <= 768:
                    raise ImageFormatError('invalid_png_palette')
            elif not 97 <= kind[0] <= 122:
                raise ImageFormatError('png_variant_unavailable')
        offset = end
    if not header or not ended or not compressed:
        raise ImageFormatError('invalid_png_structure')
    height, row_size, expected = header
    try:
        stream = zlib.decompressobj()
        decoded = stream.decompress(bytes(compressed), expected + 1)
        if len(decoded) != expected or not stream.eof or stream.unused_data or stream.unconsumed_tail:
            raise ImageFormatError('invalid_png_pixels')
        if any(decoded[i * row_size] > 4 for i in range(height)):
            raise ImageFormatError('invalid_png_filter')
    except zlib.error:
        raise ImageFormatError('invalid_png_pixels') from None


def _jpeg(data):
    offset, frame, scan, markers = 2, False, False, 0
    quantization, huffman = False, False
    while offset < len(data):
        if data[offset] != 255:
            raise ImageFormatError('invalid_jpeg_structure')
        while offset < len(data) and data[offset] == 255:
            offset += 1
        if offset >= len(data):
            break
        marker = data[offset]
        offset += 1
        markers += 1
        if markers > 16384:
            raise ImageFormatError('image_marker_limit')
        if marker == 0xD9:
            if frame and scan and quantization and huffman and offset == len(data):
                return
            break
        if marker in {0, 0xD8} or 0xD0 <= marker <= 0xD7 or offset + 2 > len(data):
            break
        length = int.from_bytes(data[offset:offset + 2], 'big')
        if length < 2 or offset + length > len(data):
            break
        value = data[offset + 2:offset + length]
        offset += length
        if marker == 0xDB:
            if not value:
                break
            quantization = True
        elif marker == 0xC4:
            if len(value) < 17:
                break
            huffman = True
        elif marker in {0xC0, 0xC2}:
            if frame or len(value) < 6 or value[0] != 8 or value[5] not in {1, 3} or len(value) != 6 + value[5] * 3:
                break
            _dimensions(int.from_bytes(value[3:5], 'big'), int.from_bytes(value[1:3], 'big'))
            frame = True
        elif 0xC0 <= marker <= 0xCF and marker not in {0xC4, 0xC8, 0xCC}:
            raise ImageFormatError('jpeg_variant_unavailable')
        elif marker == 0xDA:
            if not frame or len(value) < 4 or value[0] not in {1, 3} or len(value) != 4 + 2 * value[0]:
                break
            scan = True
            # Entropy bytes are opaque; only escaped FF/restart markers may be
            # skipped. A real marker returns to the bounded outer parser.
            while offset < len(data):
                if data[offset] != 255:
                    offset += 1
                    continue
                if offset + 1 < len(data) and (data[offset + 1] == 0 or 0xD0 <= data[offset + 1] <= 0xD7):
                    offset += 2
                    continue
                break
    raise ImageFormatError('invalid_jpeg_structure')


def detect_image(content, filename, *, max_bytes=MAX_IMAGE_BYTES):
    if (type(content) is not bytes or type(max_bytes) is not int
            or not 1 <= len(content) <= min(max_bytes, MAX_IMAGE_BYTES)):
        raise ImageFormatError('image_size_limit')
    if content.startswith(b'\x89PNG\r\n\x1a\n'):
        mime = 'image/png'
        _png(content)
    elif content.startswith(b'\xff\xd8'):
        mime = 'image/jpeg'
        _jpeg(content)
    else:
        raise ImageFormatError('image_format_unavailable')
    if not mime_matches_filename(mime, filename):
        raise ImageFormatError('image_filename_mismatch')
    return mime
