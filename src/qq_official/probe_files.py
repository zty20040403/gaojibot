"""Fixed harmless CLI samples and bounded upload receipts; no application imports.

This is not an artifact uploader: callers cannot supply bytes, names, paths or
URLs. Physical uploads use the existing account-bound RestClient protocol port.
"""
from __future__ import annotations

import base64
from dataclasses import dataclass, field
import hashlib
from io import BytesIO
import struct
import unicodedata
import zipfile
import zlib


FILE_CHECKS = ("image_upload", "pdf_upload", "zip_upload")


@dataclass(frozen=True)
class ProbeFile:
    name: str
    mime: str
    file_type: int
    content: bytes = field(repr=False)

    @property
    def summary(self):
        return {"sample_name": self.name, "sample_mime": self.mime,
                "sample_bytes": len(self.content),
                "sample_sha256": hashlib.sha256(self.content).hexdigest()}

    @property
    def payload(self):
        return {"file_type": self.file_type, "file_name": self.name,
                "file_data": base64.b64encode(self.content).decode("ascii"),
                "srv_send_msg": False}


def _png():
    def chunk(kind, data):
        return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", zlib.crc32(kind + data))
    return (b"\x89PNG\r\n\x1a\n"
            + chunk(b"IHDR", struct.pack(">IIBBBBB", 1, 1, 8, 2, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(b"\x00\x60\x90\xc0")) + chunk(b"IEND", b""))


def _pdf():
    stream = b"BT /F1 12 Tf 40 120 Td (QQ official probe - harmless sample.) Tj ET\n"
    objects = [b"<< /Type /Catalog /Pages 2 0 R >>",
               b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
               b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 300 180] /Resources << /Font << /F1 4 0 R >> >> /Contents 5 0 R >>",
               b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
               b"<< /Length " + str(len(stream)).encode() + b" >>\nstream\n" + stream + b"endstream"]
    data = bytearray(b"%PDF-1.4\n")
    offsets = [0]
    for number, body in enumerate(objects, 1):
        offsets.append(len(data))
        data.extend(str(number).encode() + b" 0 obj\n" + body + b"\nendobj\n")
    xref = len(data)
    data.extend(b"xref\n0 6\n0000000000 65535 f \n")
    for offset in offsets[1:]:
        data.extend(f"{offset:010d} 00000 n \n".encode())
    data.extend(b"trailer\n<< /Size 6 /Root 1 0 R >>\nstartxref\n" + str(xref).encode() + b"\n%%EOF\n")
    return bytes(data)


def _zip():
    stream = BytesIO()
    with zipfile.ZipFile(stream, "w", compression=zipfile.ZIP_STORED) as archive:
        item = zipfile.ZipInfo("README.txt", (1980, 1, 1, 0, 0, 0))
        item.external_attr = 0o100600 << 16
        archive.writestr(item, b"QQ official probe: fixed harmless ZIP sample.\n")
    return stream.getvalue()


def sample(check: str) -> ProbeFile:
    # Generate only internal fixed samples; never accept a user file/URL.
    if check == "image_upload":
        return ProbeFile("qq-probe.png", "image/png", 1, _png())
    if check == "pdf_upload":
        return ProbeFile("qq-probe.pdf", "application/pdf", 4, _pdf())
    if check == "zip_upload":
        return ProbeFile("qq-probe.zip", "application/zip", 4, _zip())
    raise ValueError("unknown fixed probe file")


class UnconfirmedUpload(ValueError):
    def __init__(self):
        super().__init__("unconfirmed_upload_receipt")


@dataclass(frozen=True)
class ProbeMedia:
    file_info: str = field(repr=False)
    file_uuid: str = field(repr=False)
    expires_at: float


def upload_receipt(status, body, *, started_at: float) -> ProbeMedia:
    """Same small-upload contract as native_media; conservative original time.

    No raw_url is used. An implicit message receipt conflicts with srv_send_msg
    false, so no additional send may follow. ttl=0 is the existing contract's
    cache-unbounded sentinel, capped locally; this CLI also keeps the original
    short reply/batch deadline. An incomplete or malformed response is unknown.
    """
    if (type(status) is not int or status != 200 or not isinstance(body, dict)
            or any(type(body[k]) is not int or body[k] != 0 for k in ("code", "err_code") if k in body)
            or body.get("id") not in (None, "")):
        raise UnconfirmedUpload()
    values = []
    for key, maximum in (("file_info", 65536), ("file_uuid", 1024)):
        value = body.get(key)
        try:
            valid = (isinstance(value, str) and bool(value) and len(value.encode()) <= maximum
                     and not any(unicodedata.category(c) in {"Cc", "Cf", "Cs"} for c in value))
        except UnicodeError:
            valid = False
        if not valid:
            raise UnconfirmedUpload()
        values.append(value)
    ttl = body.get("ttl")
    if type(ttl) is not int or not 0 <= ttl <= 2**31 - 1:
        raise UnconfirmedUpload()
    return ProbeMedia(*values, started_at + max(0, min(ttl or 86400, 86400) - 5))
