"""gaoji-transcribe FILE: CPU Whisper inside the conversation VM."""
import argparse
from pathlib import Path

from faster_whisper import WhisperModel


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('file')
    parser.add_argument('--model', default='base')
    parser.add_argument('--language', default=None)
    args = parser.parse_args()
    cache = Path('/work/.gaoji/whisper')
    cache.mkdir(parents=True, exist_ok=True)
    model = WhisperModel(args.model, device='cpu', compute_type='int8', cpu_threads=2,
                         download_root=str(cache))
    segments, info = model.transcribe(args.file, language=args.language, vad_filter=True)
    print(f'语言：{info.language}；时长：{info.duration:.1f} 秒', flush=True)
    for segment in segments:
        print(f'[{segment.start:.1f}–{segment.end:.1f}] {segment.text.strip()}', flush=True)


if __name__ == '__main__':
    main()
