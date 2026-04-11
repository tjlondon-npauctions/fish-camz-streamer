import json
import subprocess
from dataclasses import dataclass


@dataclass
class StreamInfo:
    video_codec: str = ""
    audio_codec: str = ""
    width: int = 0
    height: int = 0
    framerate: float = 0.0
    video_bitrate: int = 0
    audio_bitrate: int = 0
    is_h264: bool = False
    is_aac: bool = False
    can_copy: bool = False

    @property
    def resolution(self) -> str:
        if self.width and self.height:
            return f"{self.width}x{self.height}"
        return "unknown"


def probe_stream(url: str, timeout: int = 10) -> StreamInfo:
    """Probe an RTSP stream with ffprobe and return codec/format info.

    Args:
        url: RTSP URL to probe (e.g. rtsp://192.168.1.100:554/stream1)
        timeout: Seconds before giving up

    Returns:
        StreamInfo with detected stream properties

    Raises:
        RuntimeError: If ffprobe fails or stream is unreachable
    """
    cmd = [
        "ffprobe",
        "-v", "quiet",
        "-rtsp_transport", "tcp",
        "-print_format", "json",
        "-show_streams",
        url,
    ]

    try:
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=timeout,
        )
    except subprocess.TimeoutExpired:
        raise RuntimeError(f"Timed out probing stream at {url} after {timeout}s")
    except FileNotFoundError:
        raise RuntimeError("ffprobe not found. Is FFmpeg installed?")

    if result.returncode != 0:
        stderr = result.stderr.strip()
        raise RuntimeError(f"ffprobe failed: {stderr or 'unknown error'}")

    try:
        data = json.loads(result.stdout)
    except json.JSONDecodeError:
        raise RuntimeError("ffprobe returned invalid JSON")

    streams = data.get("streams", [])
    if not streams:
        raise RuntimeError(f"No streams found at {url}")

    info = StreamInfo()

    for stream in streams:
        codec_type = stream.get("codec_type", "")
        codec_name = stream.get("codec_name", "")

        if codec_type == "video" and not info.video_codec:
            info.video_codec = codec_name
            info.width = int(stream.get("width", 0))
            info.height = int(stream.get("height", 0))
            info.is_h264 = codec_name in ("h264", "H264")

            # Parse framerate from r_frame_rate (e.g. "30/1")
            r_frame_rate = stream.get("r_frame_rate", "0/1")
            try:
                num, den = r_frame_rate.split("/")
                info.framerate = float(num) / float(den) if float(den) else 0
            except (ValueError, ZeroDivisionError):
                info.framerate = 0

            bit_rate = stream.get("bit_rate")
            if bit_rate:
                info.video_bitrate = int(bit_rate)

        elif codec_type == "audio" and not info.audio_codec:
            info.audio_codec = codec_name
            info.is_aac = codec_name in ("aac", "AAC")

            bit_rate = stream.get("bit_rate")
            if bit_rate:
                info.audio_bitrate = int(bit_rate)

    # Can copy video if it's H.264 (audio handled separately)
    info.can_copy = info.is_h264

    return info
