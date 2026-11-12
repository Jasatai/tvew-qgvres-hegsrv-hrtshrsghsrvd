# It's all AI created. code is still shit, and not made to last. i do not recommend using it for anything

# Read-only music indexer — Part 1

Scans a music library for `.mp3`, `.m4a`, `.opus`, and `.mka` files and
writes their file details and available tags to a JSON index.

## Requirements

- Python 3.10+
- `mutagen` for MP3, M4A, and Opus metadata
- `ffprobe` (from FFmpeg) for MKA metadata

Install the Python dependencies:

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

## Music lookup — Part 2

Looks up each indexed track on YouTube Music and lrc.red. Lookup results are
stored separately from the original file tags.

### Requirements

- Python 3.10+
- The Part 1 dependencies
- `requests`
- `ytmusicapi`

Add `requests` and `ytmusicapi` to `requirements.txt`, then install:

    python -m pip install -r requirements.txt

### Run

First create the index using Part 1. Then run:

    python music_lookup.py indexed.json --output lookups.json

The script waits at least 5 seconds between requests to YouTube Music and
lrc.red. It writes lookup results to `lookups.json` and leaves the audio files
unchanged.

Use `--replace` to overwrite an existing output file:

    python music_lookup.py indexed.json --output lookups.json --replace

### Lookup behavior

- Searches YouTube Music using available title, artist, album, and album-artist
  tags.
- Saves a confident match's video ID, returned metadata, and YouTube URL.
- Searches lrc.red using available title, artist, album, duration, and ISRC
  information.
- Marks uncertain matches as `ambiguous` for review instead of silently
  choosing one.
- Stores lookup data separately from the original tags.

The lrc.red search endpoint or response format may need adjustment to match
its current API.
