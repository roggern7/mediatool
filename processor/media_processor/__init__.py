"""Media Tool: processamento de mídia via FFmpeg/ffprobe (V1). Ver README.md."""

from .errors import (
    FFmpegNotFoundError,
    InvalidMediaError,
    ProcessingCancelledError,
    ProcessingFailedError,
    ProcessorError,
)
from .hashing import sha256_file
from .options import ProcessingOptions
from .probe import ProbeResult
from .processor import MediaProcessor, ProcessResult
from .tools import ToolInfo

__all__ = [
    "MediaProcessor",
    "ProcessingOptions",
    "ProbeResult",
    "ProcessResult",
    "ToolInfo",
    "ProcessorError",
    "FFmpegNotFoundError",
    "InvalidMediaError",
    "ProcessingFailedError",
    "ProcessingCancelledError",
    "sha256_file",
]
