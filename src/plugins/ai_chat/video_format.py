"""Bounded MP4 metadata inspection only; decoding untrusted video stays in the VM."""
from fractions import Fraction

from .sandbox_video import MAX_SECONDS, MAX_BYTES


def duration(content: bytes) -> float:
    if not 24 <= len(content) <= MAX_BYTES:
        raise ValueError('video_size_limit')
    count = 0
    def boxes(start, end):
        nonlocal count
        while start < end:
            count += 1
            if count > 2048 or end-start < 8:
                raise ValueError('invalid_mp4_boxes')
            size, kind = int.from_bytes(content[start:start+4], 'big'), content[start+4:start+8]
            header = 8
            if size == 1:
                if end-start < 16:
                    raise ValueError('invalid_mp4_box_size')
                size, header = int.from_bytes(content[start+8:start+16], 'big'), 16
            elif size == 0:
                size = end-start
            if size < header or size > end-start:
                raise ValueError('invalid_mp4_box_size')
            yield kind, start+header, start+size
            start += size
    def seconds(start, end):
        version = content[start] if start < end else -1
        offset, width = (12,4) if version == 0 else (20,8) if version == 1 else (0,0)
        if not width or end-start < offset+4+width:
            raise ValueError('invalid_mp4_duration')
        scale = int.from_bytes(content[start+offset:start+offset+4], 'big')
        ticks = int.from_bytes(content[start+offset+4:start+offset+4+width], 'big')
        if not scale or not ticks:
            raise ValueError('invalid_mp4_duration')
        value = Fraction(ticks,scale)
        if value > MAX_SECONDS:
            raise ValueError('video_duration_limit')
        return value
    top = list(boxes(0,len(content)))
    if (sum(kind == b'ftyp' for kind,_,_ in top) != 1
        or sum(kind == b'moov' for kind,_,_ in top) != 1
        or not any(kind == b'mdat' and end > start for kind,start,end in top)):
        raise ValueError('invalid_mp4_container')
    durations, movie, tracks = [], 0, 0
    for kind,start,end in top:
        if kind != b'moov':
            continue
        for child,a,b in boxes(start,end):
            if child == b'mvhd':
                movie += 1
                durations.append(seconds(a,b))
            if child == b'trak':
                for item,c,d in boxes(a,b):
                    if item == b'mdia':
                        for part,e,f in boxes(c,d):
                            if part == b'mdhd':
                                tracks += 1
                                durations.append(seconds(e,f))
    if movie != 1 or not tracks:
        raise ValueError('missing_mp4_duration')
    return float(max(durations))
