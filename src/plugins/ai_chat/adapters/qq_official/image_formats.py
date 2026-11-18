"""Bounded PNG/JPEG artifact contract; neither MIME nor extension is evidence.

This adapter validates container structure, dimensions and the complete PNG
scanline stream, and the GIF block structure (frames stay animated on QQ). It is
not an OCR/image-content safety classifier. Animated PNG, SVG, WebP and
video/audio remain unavailable.

Ordinary files from phones and chat apps must pass: palette, low/high bit depth
and interlaced PNG, extended-sequential and CMYK JPEG, bytes after the end
marker (never decoded; downloads are served as nosniff attachments, like GIF),
and a JPEG cut off after its first complete scan header (decoders show it).
"""
import binascii
from pathlib import PurePath
import struct
import zlib
import unicodedata


MAX_IMAGE_BYTES = 10 * 1024 * 1024
MAX_IMAGE_PIXELS = 32_000_000
MAX_IMAGE_DECODED_BYTES = 128_000_000
IMAGE_MIMES = frozenset({'image/png', 'image/jpeg', 'image/gif'})
ALL_ARTIFACT_MIMES = IMAGE_MIMES | {'application/pdf', 'application/zip'}


class ImageFormatError(ValueError):
    """Fixed safe diagnostic only."""


def file_type_for_mime(mime):
    return 1 if mime.startswith('image/') else 4


def mime_matches_filename(mime, filename):
    suffix = PurePath(filename).suffix.lower()
    return suffix in {'image/png': {'.png'}, 'image/jpeg': {'.jpg', '.jpeg'}, 'image/gif': {'.gif'},
                     'application/pdf': {'.pdf'}, 'application/zip': {'.zip'}}.get(mime, set())


def canonical_artifact_filename(filename: str, *, draft: bool) -> str:
    """Host display-name normalization, never a filesystem path or authority.

    The Coordinator request and persistent manifest must use this same result;
    projections continue to compare the real stored filename exactly.
    """
    if (not isinstance(filename, str) or not filename or type(draft) is not bool
            or filename != filename.strip() or len(filename.encode("utf-8")) > 4096
            or any(c in "/\\" or unicodedata.category(c) in {"Cc", "Cf", "Cs"} for c in filename)):
        raise ImageFormatError("invalid_artifact_name")
    prefix = "草稿-" if draft else ""
    if draft and filename.startswith(prefix):
        filename = filename[len(prefix):]
    suffix = PurePath(filename).suffix
    budget = 180 - len((prefix + suffix).encode("utf-8"))
    stem = (filename[:-len(suffix)] if suffix else filename).encode("utf-8")[:budget].decode("utf-8", "ignore")
    if not stem or stem in {".", ".."}:
        raise ImageFormatError("invalid_artifact_name")
    return prefix + stem + suffix


def artifact_filename(filename, *, draft):
    return canonical_artifact_filename(filename, draft=draft)


def _dimensions(width, height):
    if not 1 <= width <= 8192 or not 1 <= height <= 8192 or width * height > MAX_IMAGE_PIXELS:
        raise ImageFormatError('image_dimension_limit')


_PNG_DEPTHS = {0: {1, 2, 4, 8, 16}, 2: {8, 16}, 3: {1, 2, 4, 8}, 4: {8, 16}, 6: {8, 16}}
_PNG_CHANNELS = {0: 1, 2: 3, 3: 1, 4: 2, 6: 4}
# Adam7 passes: (x start, y start, x step, y step).
_ADAM7 = ((0, 0, 8, 8), (4, 0, 8, 8), (0, 4, 4, 8), (2, 0, 4, 4), (0, 2, 2, 4), (1, 0, 2, 2), (0, 1, 1, 2))


def _png_rows(width, height, bits, interlace):
    """Byte length of every filtered scanline, filter byte included."""
    if not interlace:
        return [1 + (width * bits + 7) // 8] * height
    rows = []
    for x, y, dx, dy in _ADAM7:
        columns = (width - x + dx - 1) // dx if width > x else 0
        count = (height - y + dy - 1) // dy if height > y else 0
        if columns:
            rows.extend([1 + (columns * bits + 7) // 8] * count)
    return rows


def _png(data):
    offset, chunks, header, ended, after_idat = 8, 0, None, False, False
    color, palette = None, False
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
            if (depth not in _PNG_DEPTHS.get(color, ()) or method or filter_method
                    or interlace not in {0, 1}):
                raise ImageFormatError('png_variant_unavailable')
            rows = _png_rows(width, height, _PNG_CHANNELS[color] * depth, interlace)
            expected = sum(rows)
            if expected > MAX_IMAGE_DECODED_BYTES:
                raise ImageFormatError('image_decoded_limit')
            header = (rows, expected, depth)
        elif kind == b'IHDR' or kind in {b'acTL', b'fcTL', b'fdAT'}:
            raise ImageFormatError('png_variant_unavailable')
        elif kind == b'IDAT':
            if after_idat or (color == 3 and not palette):
                raise ImageFormatError('invalid_png_structure')
            compressed.extend(value)
        elif kind == b'IEND':
            if length:
                raise ImageFormatError('invalid_png_structure')
            # Bytes after IEND are never decoded.
            ended = True
            break
        else:
            if compressed:
                after_idat = True
            # Unknown critical chunks are not safe to ignore. A palette is
            # bounded by the bit depth when it indexes the pixels.
            if kind == b'PLTE':
                limit = 3 * (1 << header[2]) if color == 3 else 768
                if palette or compressed or length % 3 or not 3 <= length <= min(limit, 768):
                    raise ImageFormatError('invalid_png_palette')
                palette = True
            elif not 97 <= kind[0] <= 122:
                raise ImageFormatError('png_variant_unavailable')
        offset = end
    if not header or not ended or not compressed:
        raise ImageFormatError('invalid_png_structure')
    rows, expected, _ = header
    try:
        stream = zlib.decompressobj()
        decoded = stream.decompress(bytes(compressed), expected + 1)
        if len(decoded) != expected or not stream.eof or stream.unused_data or stream.unconsumed_tail:
            raise ImageFormatError('invalid_png_pixels')
        position = 0
        for size in rows:
            if decoded[position] > 4:
                raise ImageFormatError('invalid_png_filter')
            position += size
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
            # Bytes after EOI are never decoded.
            if frame and scan and quantization and huffman:
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
        elif marker in {0xC0, 0xC1, 0xC2}:
            # Baseline, extended sequential and progressive Huffman; grey, YCbCr or CMYK.
            if (frame or len(value) < 6 or value[0] != 8 or value[5] not in {1, 3, 4}
                    or len(value) != 6 + value[5] * 3):
                break
            _dimensions(int.from_bytes(value[3:5], 'big'), int.from_bytes(value[1:3], 'big'))
            frame = True
        elif 0xC0 <= marker <= 0xCF and marker not in {0xC4, 0xC8, 0xCC}:
            raise ImageFormatError('jpeg_variant_unavailable')
        elif marker == 0xDA:
            if not frame or len(value) < 4 or not 1 <= value[0] <= 4 or len(value) != 4 + 2 * value[0]:
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
            if offset + 1 >= len(data) and quantization and huffman:
                # Cut off inside or right after scan data: decoders show what arrived.
                return
    raise ImageFormatError('invalid_jpeg_structure')


def _gif_sub_blocks(data, offset):
    while True:
        if offset >= len(data):
            raise ImageFormatError('invalid_gif_structure')
        size = data[offset]
        offset += 1 + size
        if size == 0:
            return offset


def _gif(data):
    if len(data) < 13:
        raise ImageFormatError('invalid_gif_structure')
    _dimensions(int.from_bytes(data[6:8], 'little'), int.from_bytes(data[8:10], 'little'))
    offset = 13 + ((3 << ((data[10] & 7) + 1)) if data[10] & 0x80 else 0)
    frames = blocks = 0
    while offset < len(data):
        blocks += 1
        if blocks > 65536:
            raise ImageFormatError('image_marker_limit')
        kind = data[offset]
        if kind == 0x3B:
            # Bytes after the trailer are never decoded.
            if frames:
                return
            break
        if kind == 0x21 and offset + 2 <= len(data):
            offset = _gif_sub_blocks(data, offset + 2)
        elif kind == 0x2C and offset + 10 <= len(data):
            descriptor = data[offset + 1:offset + 10]
            _dimensions(int.from_bytes(descriptor[4:6], 'little'), int.from_bytes(descriptor[6:8], 'little'))
            offset += 10 + ((3 << ((descriptor[8] & 7) + 1)) if descriptor[8] & 0x80 else 0)
            if offset >= len(data) or not 2 <= data[offset] <= 12:
                break
            offset = _gif_sub_blocks(data, offset + 1)
            frames += 1
            if frames > 4096:
                raise ImageFormatError('image_marker_limit')
        else:
            break
    raise ImageFormatError('invalid_gif_structure')


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
    elif content[:6] in {b'GIF87a', b'GIF89a'}:
        mime = 'image/gif'
        _gif(content)
    else:
        raise ImageFormatError('image_format_unavailable')
    if not mime_matches_filename(mime, filename):
        raise ImageFormatError('image_filename_mismatch')
    return mime
