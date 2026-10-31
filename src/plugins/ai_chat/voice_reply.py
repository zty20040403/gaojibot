"""Official QQ voice: Edge TTS to Tencent SILK, with no local fallback."""
import asyncio
from io import BytesIO

import edge_tts
import miniaudio
import pysilk


async def synthesize(text):
    async with asyncio.timeout(60):
        audio = bytearray()
        async for chunk in edge_tts.Communicate(text, 'zh-CN-XiaoxiaoNeural').stream():
            if chunk['type'] == 'audio':
                audio.extend(chunk['data'])
    if not audio:
        raise ValueError('语音合成没有返回音频。')

    def encode():
        decoded = miniaudio.decode(bytes(audio), output_format=miniaudio.SampleFormat.SIGNED16,
                                   nchannels=1, sample_rate=24000)
        output = BytesIO()
        pysilk.encode(BytesIO(bytes(decoded.samples)), output, sample_rate=24000,
                      bit_rate=24000, max_internal_sample_rate=24000, tencent=True)
        return output.getvalue()
    return await asyncio.to_thread(encode)
