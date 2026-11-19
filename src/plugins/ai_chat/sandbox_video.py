"""gaoji-video FILE: prepare a bounded video in the VM; no model key or network."""
import argparse
import json
import math
from pathlib import Path
import subprocess
import tempfile
import uuid

MAX_SECONDS = 600
MAX_BYTES = 32 * 1024 * 1024
WORK_ROOT = Path('/work')


class VideoError(ValueError):
    pass


def probe_duration(path):
    response = subprocess.run(['ffprobe','-v','error','-protocol_whitelist','file,pipe',
        '-select_streams','v:0','-read_intervals','%+#1','-count_packets',
        '-show_entries','format=duration:stream=nb_read_packets',
        '-of','json',str(path)],capture_output=True,check=True,timeout=20)
    info = json.loads(response.stdout)
    seconds = float(info.get('format',{}).get('duration',0))
    if not math.isfinite(seconds) or not 0 < seconds <= MAX_SECONDS:
        raise VideoError('video_duration_limit_600_seconds')
    if not info.get('streams') or not int(info['streams'][0].get('nb_read_packets',0)):
        raise VideoError('video_stream_has_no_packets')
    return seconds


def prepare(path, output=None):
    root = WORK_ROOT.resolve(strict=True)
    source = Path(path)
    if not source.is_absolute():
        source = root / source
    source = source.resolve(strict=True)
    if not source.is_relative_to(root) or not source.is_file():
        raise VideoError('video_source_must_be_under_work')
    probe_duration(source)
    destination = Path(output) if output else root / '.gaoji/video' / (uuid.uuid4().hex+'.mp4')
    if not destination.is_absolute():
        destination = root / destination
    destination = destination.resolve()
    if not destination.is_relative_to(root) or destination.exists():
        raise VideoError('video_output_must_be_new_under_work')
    destination.parent.mkdir(parents=True,exist_ok=True)
    with tempfile.TemporaryDirectory(dir=destination.parent) as directory:
        temporary = Path(directory)/'video.mp4'
        temporary.touch(mode=0o600)
        subprocess.run(['ffmpeg','-v','error','-nostdin','-protocol_whitelist','file,pipe','-i',str(source),
            '-map','0:v:0','-map','0:a:0?','-vf',
            "fps=1:eof_action=pass,scale=w='min(640,iw)':h='min(640,ih)':force_original_aspect_ratio=decrease,pad=ceil(iw/2)*2:ceil(ih/2)*2",
            '-c:v','libx264','-preset','veryfast','-crf','28','-bf','0','-pix_fmt','yuv420p',
            '-c:a','aac','-ac','1','-ar','32000','-t',str(MAX_SECONDS),'-movflags','+faststart',
            '-map_metadata','-1','-y',str(temporary)],capture_output=True,check=True,timeout=180)
        size = temporary.stat().st_size
        if not 0 < size <= MAX_BYTES:
            raise VideoError('prepared_video_exceeds_32_mib')
        seconds = probe_duration(temporary)
        temporary.replace(destination)
        return {'path':str(destination),'seconds':seconds,'byte_size':size}


def main():
    parser = argparse.ArgumentParser(description='在 VM 内准备最长 10 分钟、1fps、长边 640、单声道 MP4。')
    parser.add_argument('path')
    parser.add_argument('--output')
    args = parser.parse_args()
    try:
        print(json.dumps(prepare(args.path,args.output),ensure_ascii=False))
    except (OSError, ValueError, subprocess.SubprocessError) as error:
        reason = str(error) if isinstance(error,VideoError) else type(error).__name__
        parser.exit(1, f'视频准备失败：{reason}。请检查本地文件、10 分钟限制和 ffmpeg；没有发送到模型。\n')


if __name__ == '__main__':
    main()
