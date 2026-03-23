"""
将任意 iPhone 音频格式（M4A/AAC/WAV 等）转换为
STT 严格要求的格式：1ch / 16bit / 16kHz WAV bytes。
"""
import io
import os
import wave
import tempfile
import subprocess


class AudioConversionError(Exception):
    pass


def detect_format_from_content_type(content_type: str) -> str:
    """从 HTTP Content-Type 推断音频格式，Siri Shortcuts 默认返回 m4a。"""
    ct = (content_type or "").lower()
    if "wav" in ct:
        return "wav"
    if "aac" in ct:
        return "aac"
    if "mp3" in ct or "mpeg" in ct:
        return "mp3"
    return "m4a"  # Siri Shortcuts "Record Audio" 最常见格式


def is_correct_wav_format(wav_bytes: bytes) -> bool:
    """快速验证 WAV 是否已经是目标格式，避免不必要的 ffmpeg 转码。"""
    try:
        with wave.open(io.BytesIO(wav_bytes), "rb") as wf:
            return (
                wf.getnchannels() == 1
                and wf.getsampwidth() == 2
                and wf.getframerate() == 16000
            )
    except Exception:
        return False


def convert_to_wav_bytes(input_bytes: bytes, input_format: str = "m4a") -> bytes:
    """
    用 ffmpeg 将任意格式音频转为 16kHz/16bit/单声道 WAV bytes。
    写临时文件 → ffmpeg 转码 → 读输出 → 清理临时文件。
    """
    suffix_in = f".{input_format}"
    f_in = tempfile.NamedTemporaryFile(suffix=suffix_in, delete=False)
    path_in = f_in.name
    path_out = path_in.replace(suffix_in, "_out.wav")

    try:
        f_in.write(input_bytes)
        f_in.close()

        cmd = [
            "ffmpeg", "-y",
            "-i", path_in,
            "-ar", "16000",
            "-ac", "1",
            "-sample_fmt", "s16",
            "-f", "wav",
            path_out,
        ]
        result = subprocess.run(cmd, capture_output=True, timeout=30)
        if result.returncode != 0:
            err = result.stderr.decode(errors="replace")
            raise AudioConversionError(f"ffmpeg 转码失败: {err}")

        with open(path_out, "rb") as f_out:
            return f_out.read()
    finally:
        if os.path.exists(path_in):
            os.unlink(path_in)
        if os.path.exists(path_out):
            os.unlink(path_out)


def smart_convert(input_bytes: bytes, content_type: str) -> bytes:
    """
    对外统一接口：智能判断是否需要转码。
    - 已是正确格式的 WAV → 直接返回
    - 其他格式 → ffmpeg 转码
    """
    fmt = detect_format_from_content_type(content_type)
    if fmt == "wav" and is_correct_wav_format(input_bytes):
        return input_bytes
    return convert_to_wav_bytes(input_bytes, input_format=fmt)
