"""ffmpeg helpers: probe, decode to numpy, encode browser-safe MP4, thumbnails."""

import os
import re
import subprocess

import numpy as np

_bin = {"path": None}


def ffmpeg_bin():
    if not _bin["path"]:
        import imageio_ffmpeg

        _bin["path"] = imageio_ffmpeg.get_ffmpeg_exe()
    return _bin["path"]


def probe(path):
    result = subprocess.run([ffmpeg_bin(), "-hide_banner", "-i", path], capture_output=True, text=True, timeout=60)
    text = result.stderr
    size = re.search(r"Video:.*?(\d{2,5})x(\d{2,5})", text)
    fps = re.search(r"(\d+(?:\.\d+)?) fps", text)
    duration = re.search(r"Duration: (\d+):(\d+):(\d+(?:\.\d+)?)", text)
    if not size:
        raise ValueError("no video stream found")
    seconds = 0.0
    if duration:
        h, m, s = duration.groups()
        seconds = int(h) * 3600 + int(m) * 60 + float(s)
    return {
        "width": int(size.group(1)),
        "height": int(size.group(2)),
        "fps": float(fps.group(1)) if fps else 30.0,
        "duration": seconds,
    }


def work_size(width, height, target_h=720):
    if height <= target_h:
        h = height - height % 2
        w = width - width % 2
        return w, h
    w = int(round(width * target_h / height / 2)) * 2
    return w, target_h


def transcode(src, dst, width, height, max_seconds=15.0):
    _run([
        ffmpeg_bin(), "-v", "error", "-y", "-i", src, "-t", str(max_seconds),
        "-vf", f"scale={width}:{height}:flags=area", "-c:v", "libx264", "-preset", "veryfast", "-crf", "20",
        "-pix_fmt", "yuv420p", "-movflags", "+faststart", "-an", dst,
    ])


def read_frames(path, width, height):
    raw = subprocess.run(
        [ffmpeg_bin(), "-v", "error", "-i", path, "-vf", f"scale={width}:{height}", "-f", "rawvideo", "-pix_fmt", "bgr24", "-"],
        capture_output=True, check=True, timeout=300,
    ).stdout
    frames = np.frombuffer(raw, dtype=np.uint8)
    count = frames.size // (width * height * 3)
    return frames[: count * width * height * 3].reshape(count, height, width, 3).copy()


def write_video(frames, fps, out_path):
    count, height, width, _ = frames.shape
    tmp = out_path + ".part.mp4"
    process = subprocess.Popen(
        [
            ffmpeg_bin(), "-v", "error", "-y", "-f", "rawvideo", "-pix_fmt", "bgr24", "-s", f"{width}x{height}",
            "-r", f"{fps:.4f}", "-i", "-", "-c:v", "libx264", "-preset", "veryfast", "-crf", "20",
            "-pix_fmt", "yuv420p", "-movflags", "+faststart", "-an", tmp,
        ],
        stdin=subprocess.PIPE, stderr=subprocess.PIPE,
    )
    try:
        for frame in frames:
            process.stdin.write(np.ascontiguousarray(frame).tobytes())
        process.stdin.close()
        process.wait(timeout=300)
    finally:
        if process.poll() is None:
            process.kill()
    if process.returncode != 0:
        raise RuntimeError("ffmpeg encode failed")
    os.replace(tmp, out_path)


def thumbnail(src, out_path, at=1.0, width=320):
    _run([
        ffmpeg_bin(), "-v", "error", "-y", "-ss", str(at), "-i", src, "-frames:v", "1",
        "-vf", f"scale={width}:-2", "-q:v", "4", out_path,
    ], timeout=60)


def _run(cmd, timeout=300):
    result = subprocess.run(cmd, capture_output=True, timeout=timeout)
    if result.returncode != 0:
        raise RuntimeError("ffmpeg failed")
