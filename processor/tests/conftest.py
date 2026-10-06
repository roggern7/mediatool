"""Fixtures: mídia sintética gerada com o próprio ffmpeg (uma vez por sessão)."""

from __future__ import annotations

import shutil
import struct
import subprocess
from pathlib import Path

import pytest

from media_processor import MediaProcessor

FFMPEG = shutil.which("ffmpeg")
FFPROBE = shutil.which("ffprobe")

requires_ffmpeg = pytest.mark.skipif(not (FFMPEG and FFPROBE), reason="ffmpeg/ffprobe não estão no PATH")


def ff(*args: str) -> None:
    subprocess.run([FFMPEG, "-hide_banner", "-nostdin", "-y", "-v", "error", *args], check=True)


def exif_app1(orientation: int = 6, make: bytes = b"EvilCam") -> bytes:
    """Segmento APP1 EXIF mínimo (TIFF little-endian, IFD0 com Make e Orientation)."""
    make = make + b"\x00"
    count = 2
    ifd_offset = 8
    data_offset = ifd_offset + 2 + count * 12 + 4
    entry_make = struct.pack("<HHII", 0x010F, 2, len(make), data_offset)
    entry_orient = struct.pack("<HHIHH", 0x0112, 3, 1, orientation, 0)
    tiff = b"II*\x00" + struct.pack("<I", ifd_offset) + struct.pack("<H", count)
    tiff += entry_make + entry_orient + struct.pack("<I", 0) + make
    payload = b"Exif\x00\x00" + tiff
    return b"\xff\xe1" + struct.pack(">H", len(payload) + 2) + payload


GRADIENT = "geq=lum='X*4':cb=128:cr=128"  # luminância cresce da esquerda para a direita (assimétrico)


@pytest.fixture(scope="session")
def media(tmp_path_factory) -> dict[str, Path]:
    if not (FFMPEG and FFPROBE):
        pytest.skip("ffmpeg/ffprobe não estão no PATH")
    d = tmp_path_factory.mktemp("media")
    m: dict[str, Path] = {}

    m["av"] = d / "av.mp4"
    ff("-f", "lavfi", "-i", "testsrc2=size=640x360:rate=30:duration=3",
       "-f", "lavfi", "-i", "sine=frequency=440:duration=3",
       "-c:v", "libx264", "-preset", "ultrafast", "-pix_fmt", "yuv420p", "-c:a", "aac", "-shortest",
       "-metadata", "title=Titulo Secreto", "-metadata", "comment=Comentario Secreto",
       "-metadata", "creation_time=2020-01-01T00:00:00Z", "-metadata", "location=+12.3456-045.6789/",
       "-metadata", "encoder=EncoderSecreto", "-metadata:s:v", "handler_name=HandlerSecreto",
       str(m["av"]))

    m["noaudio"] = d / "noaudio.mp4"
    ff("-f", "lavfi", "-i", "testsrc2=size=640x360:rate=30:duration=2",
       "-c:v", "libx264", "-preset", "ultrafast", "-pix_fmt", "yuv420p", str(m["noaudio"]))

    m["rot90"] = d / "rot90.mp4"
    ff("-display_rotation", "90", "-i", str(m["av"]), "-c", "copy", str(m["rot90"]))

    m["odd"] = d / "odd.mp4"
    ff("-f", "lavfi", "-i", "testsrc2=size=321x241:rate=25:duration=2",
       "-c:v", "libx264", "-preset", "ultrafast", "-pix_fmt", "yuv444p", str(m["odd"]))

    m["gradient"] = d / "gradient.mp4"
    ff("-f", "lavfi", "-i", f"nullsrc=size=64x48:rate=30:duration=2,{GRADIENT},format=yuv420p",
       "-c:v", "libx264", "-preset", "ultrafast", "-crf", "0", "-pix_fmt", "yuv420p", str(m["gradient"]))

    m["png"] = d / "gradient.png"
    ff("-f", "lavfi", "-i", f"nullsrc=size=64x48,{GRADIENT},format=gray", "-frames:v", "1", "-update", "1",
       str(m["png"]))

    plain = d / "plain.jpg"
    ff("-f", "lavfi", "-i", "testsrc2=size=320x240", "-frames:v", "1", "-update", "1", "-q:v", "3", str(plain))
    data = plain.read_bytes()
    m["exif_jpg"] = d / "exif.jpg"
    m["exif_jpg"].write_bytes(data[:2] + exif_app1(6) + data[2:])

    m["exif_jpg_upright"] = d / "exif_upright.jpg"
    m["exif_jpg_upright"].write_bytes(data[:2] + exif_app1(1) + data[2:])

    m["invalid"] = d / "invalid.mp4"
    m["invalid"].write_text("isto não é um vídeo\n" * 20, encoding="utf-8")

    m["weird"] = d / "-evil ação 日本 vídeo.mp4"
    shutil.copyfile(m["av"], m["weird"])

    m["long"] = d / "long.mp4"
    ff("-f", "lavfi", "-i", "testsrc2=size=1280x720:rate=30:duration=20",
       "-f", "lavfi", "-i", "sine=frequency=440:duration=20",
       "-c:v", "libx264", "-preset", "ultrafast", "-pix_fmt", "yuv420p", "-c:a", "aac", "-shortest",
       str(m["long"]))
    return m


@pytest.fixture(scope="session")
def mp() -> MediaProcessor:
    return MediaProcessor()


def gray_frame(path: Path, width: int, height: int) -> bytes:
    """Primeiro frame como rawvideo gray em width x height."""
    out = subprocess.run(
        [FFMPEG, "-hide_banner", "-nostdin", "-v", "error", "-i", str(path), "-frames:v", "1",
         "-vf", f"scale={width}:{height}", "-f", "rawvideo", "-pix_fmt", "gray", "-"],
        check=True, stdout=subprocess.PIPE,
    ).stdout
    assert len(out) == width * height
    return out


def top_level_atoms(path: Path) -> list[str]:
    names = []
    with open(path, "rb") as fh:
        while True:
            header = fh.read(8)
            if len(header) < 8:
                break
            size, name = struct.unpack(">I4s", header)
            if size == 1:
                size = struct.unpack(">Q", fh.read(8))[0]
                fh.seek(size - 16, 1)
            elif size == 0:
                names.append(name.decode("latin-1"))
                break
            else:
                fh.seek(size - 8, 1)
            names.append(name.decode("latin-1"))
    return names
