"""Exceções do processor. Todas derivam de ProcessorError e têm um `code` estável."""

from __future__ import annotations


class ProcessorError(Exception):
    code = "processor_error"

    def __init__(self, message: str):
        super().__init__(message)
        self.message = message


class FFmpegNotFoundError(ProcessorError):
    code = "ffmpeg_not_found"


class InvalidMediaError(ProcessorError):
    code = "invalid_media"


class ProcessingFailedError(ProcessorError):
    code = "processing_failed"

    def __init__(self, message: str, *, returncode: int | None = None, stderr: str = ""):
        super().__init__(message)
        self.returncode = returncode
        self.stderr = stderr


class ProcessingCancelledError(ProcessorError):
    code = "cancelled"
