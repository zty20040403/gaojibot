"""PNG/JPEG contract and real M6 wire/vault compatibility; no PG or network."""
import binascii
import base64
import struct
import unittest
import zlib

from src.plugins.ai_chat.adapters.qq_official.file_artifacts import detect_content
from src.plugins.ai_chat.adapters.qq_official.image_formats import (
    ImageFormatError, artifact_filename, detect_image, file_type_for_mime,
)


def chunk(kind, data):
    return len(data).to_bytes(4, 'big') + kind + data + (binascii.crc32(kind + data) & 0xffffffff).to_bytes(4, 'big')


def png(width=1, height=1, pixels=b'\0\x20\x40\x80', **kwargs):
    header = struct.pack('>IIBBBBB', width, height, 8, 2, 0, 0, 0)
    return b'\x89PNG\r\n\x1a\n' + chunk(b'IHDR', header) + chunk(b'IDAT', zlib.compress(pixels)) + chunk(b'IEND', b'')


def jpeg():
    # Actual 1x1 JPEG converted offline from the PNG fixture by system sips.
    return base64.b64decode('/9j/4AAQSkZJRgABAQAASABIAAD/4QBMRXhpZgAATU0AKgAAAAgAAYdpAAQAAAABAAAAGgAAAAAAA6ABAAMAAAABAAEAAKACAAQAAAABAAAAAaADAAQAAAABAAAAAQAAAAD/7QA4UGhvdG9zaG9wIDMuMAA4QklNBAQAAAAAAAA4QklNBCUAAAAAABDUHYzZjwCyBOmACZjs+EJ+/8AAEQgAAQABAwEiAAIRAQMRAf/EAB8AAAEFAQEBAQEBAAAAAAAAAAABAgMEBQYHCAkKC//EALUQAAIBAwMCBAMFBQQEAAABfQECAwAEEQUSITFBBhNRYQcicRQygZGhCCNCscEVUtHwJDNicoIJChYXGBkaJSYnKCkqNDU2Nzg5OkNERUZHSElKU1RVVldYWVpjZGVmZ2hpanN0dXZ3eHl6g4SFhoeIiYqSk5SVlpeYmZqio6Slpqeoqaqys7S1tre4ubrCw8TFxsfIycrS09TV1tfY2drh4uPk5ebn6Onq8fLz9PX29/j5+v/EAB8BAAMBAQEBAQEBAQEAAAAAAAABAgMEBQYHCAkKC//EALURAAIBAgQEAwQHBQQEAAECdwABAgMRBAUhMQYSQVEHYXETIjKBCBRCkaGxwQkjM1LwFWJy0QoWJDThJfEXGBkaJicoKSo1Njc4OTpDREVGR0hJSlNUVVZXWFlaY2RlZmdoaWpzdHV2d3h5eoKDhIWGh4iJipKTlJWWl5iZmqKjpKWmp6ipqrKztLW2t7i5usLDxMXGx8jJytLT1NXW19jZ2uLj5OXm5+jp6vLz9PX29/j5+v/bAEMAAgICAgICAwICAwUDAwMFBgUFBQUGCAYGBgYGCAoICAgICAgKCgoKCgoKCgwMDAwMDA4ODg4ODw8PDw8PDw8PD//bAEMBAgICBAQEBwQEBxALCQsQEBAQEBAQEBAQEBAQEBAQEBAQEBAQEBAQEBAQEBAQEBAQEBAQEBAQEBAQEBAQEBAQEP/dAAQAAf/aAAwDAQACEQMRAD8A/Peiiiv6UPys/9k=')


class ImageTests(unittest.TestCase):
    def test_png_structure_size_and_existing_artifact_detection(self):
        self.assertEqual(detect_image(png(), '实际.PNG'), 'image/png')
        self.assertEqual(detect_content(png(), 'actual.png', 1024), 'image/png')
        self.assertEqual(file_type_for_mime('image/png'), 1)
        self.assertEqual(file_type_for_mime('application/pdf'), 4)

    def test_jpeg_baseline_structure_and_extensions(self):
        for name in ('photo.jpg', 'photo.JPEG'):
            self.assertEqual(detect_image(jpeg(), name), 'image/jpeg')

    def test_magic_suffix_mime_polyglot_and_trailer_rejected(self):
        for content, name in ((png(), 'photo.jpg'), (jpeg(), 'photo.png'),
                              (png() + b'<html>', 'a.png'), (jpeg() + b'junk', 'a.jpg'),
                              (b'<svg/>', 'a.png'), (b'GIF89a', 'a.gif')):
            with self.subTest(name=name), self.assertRaises(ImageFormatError):
                detect_image(content, name)

    def test_truncation_crc_and_fake_image_rejected(self):
        for content in (png()[:-1], png()[:8], png()[:20] + b'broken' + png()[26:], jpeg()[:-2], b'\xff\xd8\xff\xd9'):
            with self.subTest(content=content[:10]), self.assertRaises(ImageFormatError):
                detect_image(content, 'a.png' if content.startswith(b'\x89') else 'a.jpg')

    def test_dimensions_and_decompression_bomb_limits(self):
        for content in (png(width=9000), png(width=5000, height=5000), png(pixels=b'x' * 1000000)):
            with self.assertRaises(ImageFormatError):
                detect_image(content, 'a.png')

    def test_apng_palette_and_interlaced_not_implicitly_enabled(self):
        content = png()
        with self.assertRaises(ImageFormatError):
            detect_image(content[:33] + chunk(b'acTL', b'\0' * 8) + content[33:], 'a.png')
        header = struct.pack('>IIBBBBB', 1, 1, 8, 3, 0, 0, 0)
        with self.assertRaises(ImageFormatError):
            detect_image(content[:8] + chunk(b'IHDR', header) + content[33:], 'a.png')

    def test_image_byte_budget_and_bad_filter(self):
        with self.assertRaises(ImageFormatError):
            detect_image(png(), 'a.png', max_bytes=5)
        with self.assertRaises(ImageFormatError):
            detect_image(png(pixels=b'\x05abc'), 'a.png')

    def test_draft_filename_same_mapping_and_old_pdf_zip(self):
        self.assertEqual(artifact_filename('图.png', draft=True), '草稿-图.png')
        self.assertEqual(artifact_filename('a.pdf', draft=True), '草稿-a.pdf')
        self.assertEqual(artifact_filename('a.zip', draft=False), 'a.zip')
        name = artifact_filename('字' * 100 + '.jpeg', draft=True)
        self.assertLessEqual(len(name.encode()), 180)
        self.assertTrue(name.startswith('草稿-') and name.endswith('.jpeg'))
        for name in ('../a.png', 'a\0.png'):
            with self.assertRaises(ValueError):
                artifact_filename(name, draft=False)





if __name__ == '__main__':
    unittest.main()
