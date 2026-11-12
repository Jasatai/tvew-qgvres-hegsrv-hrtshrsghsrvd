#!/usr/bin/env python3
"""Read-only music-library scanner for Part 1."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from mutagen import File as MutagenFile


SUPPORTED_EXTENSIONS = {".mp3", ".m4a", ".opus", ".mka"}
ID_NAMESPACE = uuid.UUID("69fd797e-b3c9-4e71-8a0e-15f0e9ac682a")


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def text_value(value: Any) -> str:
    """Convert common Mutagen/ffprobe tag values to a plain string."""
    if value is None:
        return ""
    if isinstance(value, (list, tuple)):
        if not value:
            return ""
        # MP4 track/disc values are commonly (number, total).
        value = value[0]
        if isinstance(value, (list, tuple)):
            return str(value[0]) if value else ""
    if isinstance(value, bytes):
        return value.decode("utf-8", errors="replace").strip()
    if hasattr(value, "text"):  # ID3 frames, for example
        return text_value(value.text)
    return str(value).strip()


def normalized_key(key: Any) -> str:
    return "".join(ch for ch in str(key).casefold() if ch.isalnum())


def first_tag(tags: Any, aliases: tuple[str, ...]) -> str:
    """Find a tag without assuming a particular tag-container implementation."""
    if not tags:
        return ""

    wanted = {normalized_key(alias) for alias in aliases}

    try:
        items = tags.items()
    except AttributeError:
        return ""

    for key, value in items:
        key_text = str(key)
        normalized = normalized_key(key_text)

        # Match exact normalized keys and common ID3 frame IDs.
        if normalized in wanted or key_text.upper() in aliases:
            result = text_value(value)
            if result:
                return result

        # Apple’s freeform iTunes ISRC tag.
        if any(alias.casefold() in key_text.casefold() for alias in aliases):
            result = text_value(value)
            if result:
                return result

    return ""


def read_mutagen(path: Path) -> tuple[dict[str, Any], str]:
    audio = MutagenFile(path, easy=False)
    if audio is None:
        raise ValueError("Mutagen could not identify this audio file")

    tags = getattr(audio, "tags", None)
    info = getattr(audio, "info", None)
    duration = getattr(info, "length", None)

    metadata = {
        "title": first_tag(tags, ("TITLE", "TIT2", "©nam")),
        "artist": first_tag(tags, ("ARTIST", "TPE1", "©ART")),
        "album": first_tag(tags, ("ALBUM", "TALB", "©alb")),
        "album_artist": first_tag(
            tags, ("ALBUMARTIST", "ALBUM ARTIST", "TPE2", "aART")
        ),
        "track_number": first_tag(tags, ("TRACKNUMBER", "TRACK", "TRCK", "trkn")),
        "disc_number": first_tag(tags, ("DISCNUMBER", "DISC", "TPOS", "disk")),
        "isrc": first_tag(
            tags, ("ISRC", "TSRC", "----:com.apple.iTunes:ISRC")
        ),
        "duration_seconds": (
            round(float(duration), 3) if duration is not None else ""
        ),
    }
    return metadata, ""


def read_mka_with_ffprobe(path: Path) -> tuple[dict[str, Any], str]:
    """Read MKA tags and duration through ffprobe; never invokes an encoder."""
    executable = shutil.which("ffprobe")
    if not executable:
        raise RuntimeError(
            "ffprobe was not found; install FFmpeg to scan MKA metadata"
        )

    command = [
        executable,
        "-v", "error",
        "-show_entries",
        "format=duration:format_tags:stream=duration:stream_tags",
        "-of", "json",
        str(path),
    ]
    result = subprocess.run(
        command,
        check=True,
        capture_output=True,
        text=True,
        timeout=60,
    )
    data = json.loads(result.stdout)

    # Container-level tags are preferred; stream tags fill missing values.
    tags: dict[str, Any] = {}
    format_info = data.get("format", {})
    tags.update(format_info.get("tags", {}) or {})
    streams = data.get("streams", []) or []
    for stream in streams:
        for key, value in (stream.get("tags", {}) or {}).items():
            tags.setdefault(key, value)

    duration = format_info.get("duration", "")
    if not duration:
        for stream in streams:
            if stream.get("duration"):
                duration = stream["duration"]
                break

    try:
        duration_value: float | str = round(float(duration), 3) if duration else ""
    except (TypeError, ValueError):
        duration_value = ""

    metadata = {
        "title": first_tag(tags, ("TITLE",)),
        "artist": first_tag(tags, ("ARTIST",)),
        "album": first_tag(tags, ("ALBUM",)),
        "album_artist": first_tag(tags, ("ALBUMARTIST", "ALBUM ARTIST")),
        "track_number": first_tag(tags, ("TRACKNUMBER", "TRACK")),
        "disc_number": first_tag(tags, ("DISCNUMBER", "DISC")),
        "isrc": first_tag(tags, ("ISRC",)),
        "duration_seconds": duration_value,
    }
    return metadata, ""


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as file:
        for chunk in iter(lambda: file.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def scan_track(path: Path, root: Path) -> dict[str, Any]:
    relative_path = path.relative_to(root).as_posix()
    stable_id = str(uuid.uuid5(ID_NAMESPACE, relative_path))
    stat = path.stat()

    record: dict[str, Any] = {
        "track_id": stable_id,
        "original_path": str(path.resolve()),
        "relative_path": relative_path,
        "file": {
            "extension": path.suffix.lower(),
            "size_bytes": stat.st_size,
            "modified_time_utc": datetime.fromtimestamp(
                stat.st_mtime, timezone.utc
            ).isoformat(),
            "modified_time_ns": stat.st_mtime_ns,
            "sha256": "",
        },
        "original_tags": {
            "title": "",
            "artist": "",
            "album": "",
            "album_artist": "",
            "track_number": "",
            "disc_number": "",
            "duration_seconds": "",
            "isrc": "",
        },
        "scan_error": "",
        "duplicate_group": "",
    }

    try:
        if path.suffix.lower() == ".mka":
            metadata, _ = read_mka_with_ffprobe(path)
        else:
            metadata, _ = read_mutagen(path)
        record["original_tags"].update(metadata)
    except Exception as exc:
        record["scan_error"] = f"Metadata read failed: {type(exc).__name__}: {exc}"

    try:
        record["file"]["sha256"] = sha256_file(path)
    except OSError as exc:
        message = f"SHA-256 read failed: {type(exc).__name__}: {exc}"
        record["scan_error"] = (
            f"{record['scan_error']}; {message}" if record["scan_error"] else message
        )

    return record


def write_index(output: Path, document: dict[str, Any], replace: bool) -> None:
    output.parent.mkdir(parents=True, exist_ok=True)

    if output.exists() and not replace:
        raise FileExistsError(
            f"{output} already exists. Use --replace-index to update the index."
        )

    # Write a temporary file beside the index, then atomically install it.
    temp_name: str | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            dir=output.parent,
            prefix=f".{output.name}.",
            suffix=".tmp",
            delete=False,
        ) as temp:
            temp_name = temp.name
            json.dump(document, temp, ensure_ascii=False, indent=2)
            temp.write("\n")
            temp.flush()
            os.fsync(temp.fileno())

        if output.exists() and not replace:
            raise FileExistsError(
                f"{output} already exists. Use --replace-index to update the index."
            )
        os.replace(temp_name, output)
        temp_name = None
    finally:
        if temp_name:
            try:
                os.unlink(temp_name)
            except FileNotFoundError:
                pass


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Read-only scan of MP3, M4A, Opus, and MKA files."
    )
    parser.add_argument("library", type=Path, help="Music-library directory")
    parser.add_argument(
        "--output",
        type=Path,
        default=Path("indexed.json"),
        help="Index output path (default: ./indexed.json)",
    )
    parser.add_argument(
        "--replace-index",
        action="store_true",
        help="Allow replacing the existing index file; audio files are never changed",
    )
    args = parser.parse_args()

    root = args.library.expanduser().resolve()
    output = args.output.expanduser().resolve()

    if not root.is_dir():
        parser.error(f"Library directory does not exist: {root}")

    # Do not follow symlinked files, which might point outside the library.
    paths = sorted(
        (
            path for path in root.rglob("*")
            if path.is_file()
            and not path.is_symlink()
            and path.suffix.lower() in SUPPORTED_EXTENSIONS
        ),
        key=lambda p: p.relative_to(root).as_posix().casefold(),
    )

    def without_artwork(value: Any) -> Any:
        if isinstance(value, dict):
            cleaned = {}
            for key, item in value.items():
                normalized = "".join(
                    char for char in str(key).casefold() if char.isalnum()
                )
                if any(marker in normalized for marker in (
                    "artwork", "albumart", "coverart", "picture", "image", "apic", "covr", "cover"
                )):
                    continue
                cleaned[key] = without_artwork(item)
            return cleaned
        if isinstance(value, list):
            return [without_artwork(item) for item in value]
        return value

    tracks = [
        without_artwork(scan_track(path, root))
        for path in paths
    ]
    # Group identical content hashes. Empty hashes are not treated as duplicates.
    hash_groups: dict[str, list[dict[str, Any]]] = {}
    for track in tracks:
        file_hash = track["file"]["sha256"]
        if file_hash:
            hash_groups.setdefault(file_hash, []).append(track)

    duplicate_count = 0
    for file_hash, group in hash_groups.items():
        if len(group) > 1:
            duplicate_group = f"sha256:{file_hash}"
            for track in group:
                track["duplicate_group"] = duplicate_group
                duplicate_count += 1

    document = {
        "schema_version": 1,
        "generated_at_utc": utc_now(),
        "library_root": str(root),
        "supported_extensions": sorted(SUPPORTED_EXTENSIONS),
        "track_count": len(tracks),
        "tracks": tracks,
    }

    try:
        write_index(output, document, args.replace_index)
    except (OSError, FileExistsError) as exc:
        print(f"Could not write index: {exc}", file=sys.stderr)
        return 1

    print(f"Indexed {len(tracks)} audio file(s) to {output}")
    print(f"Files with duplicate content: {duplicate_count}")
    print("Audio files were opened for reading only.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
