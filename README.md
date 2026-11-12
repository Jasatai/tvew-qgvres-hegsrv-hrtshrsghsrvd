# It's all AI created and looked over by me. code is still shit, and not made to last. i do not recomend using it for anything
# Read-only music indexer — Part 1

Scans a music library for `.mp3`, `.m4a`, `.opus`, and `.mka` files and
writes their file details and available tags to a JSON index.

## Requirements

- Python 3.10+
- `mutagen` for MP3, M4A, and Opus metadata
- `ffprobe` (from FFmpeg) for MKA metadata

Install the Python dependency:

    python -m pip install -r requirements.txt

## Run

From this project directory:

    python music_indexer.py "/path/to/music-library"

By default, this creates `indexed.json` in the current working directory.
Choose another output location with:

    python music_indexer.py "/path/to/music-library" --output "/path/to/indexed.json"

If the output index already exists, the program refuses to replace it unless
you explicitly pass `--replace-index`:

    python music_indexer.py "/path/to/music-library" --replace-index

## Index behavior

- Missing text tags are stored as empty strings; tags are not guessed.
- Each track gets an ID based on its library-relative path.
- File size, modification time, and SHA-256 are recorded.
- Matching SHA-256 values are grouped as duplicate content.
- MKA scanning needs `ffprobe`; if unavailable, the file is still indexed and
  the issue is recorded in `scan_error`.
- Symlinked files are skipped.
- Original audio files are only read. The scanner does not tag, rename, move,
  delete, or rewrite them.

## Project outline
1. **Scan the music library**
   - Find supported audio files: MP3, M4A, Opus, and MKA.
   - Read available tags, including title, artist, album, album artist, track/disc number, duration, and ISRC.
   - Record each original file path. Leave missing tag values empty rather than guessing.
