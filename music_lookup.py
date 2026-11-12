#!/usr/bin/env python3
"""Step 2: look up indexed tracks on YouTube Music and lrc.red."""

from __future__ import annotations

import argparse
import copy
import json
import os
import re
import sys
import time
from difflib import SequenceMatcher
from pathlib import Path
from typing import Any
from urllib.parse import quote, urljoin

import requests
from ytmusicapi import YTMusic


DELAY_SECONDS = 5.0
DEFAULT_LRC_SEARCH_URL = "https://lrc.red/api/search"
USER_AGENT = "MusicLibraryIndexer/1.0 (personal library metadata lookup)"


class RequestGate:
    """Serialize requests and enforce a minimum start-to-start interval."""

    def __init__(self, delay: float = DELAY_SECONDS) -> None:
        self.delay = delay
        self.last_started: float | None = None

    def wait(self) -> None:
        now = time.monotonic()
        if self.last_started is not None:
            remaining = self.delay - (now - self.last_started)
            if remaining > 0:
                time.sleep(remaining)
        self.last_started = time.monotonic()


def normalized(value: Any) -> str:
    text = str(value or "").casefold()
    return re.sub(r"[^a-z0-9]+", " ", text).strip()


def similarity(a: Any, b: Any) -> float:
    a_norm, b_norm = normalized(a), normalized(b)
    if not a_norm or not b_norm:
        return 0.0
    return SequenceMatcher(None, a_norm, b_norm).ratio()


def as_text(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, (str, int, float)):
        return str(value)
    return ""


def result_artist(item: dict[str, Any]) -> str:
    artists = item.get("artists", [])
    if isinstance(artists, list):
        return ", ".join(
            as_text(artist.get("name", "")) if isinstance(artist, dict)
            else as_text(artist)
            for artist in artists
        )
    return as_text(artists)


def result_album(item: dict[str, Any]) -> str:
    album = item.get("album", "")
    return as_text(album.get("name", "")) if isinstance(album, dict) else as_text(album)


def duration_seconds(value: Any) -> float | None:
    try:
        number = float(value)
        return number if number > 0 else None
    except (TypeError, ValueError):
        return None


def duration_from_youtube(item: dict[str, Any]) -> float | None:
    seconds = duration_seconds(item.get("duration_seconds"))
    if seconds is not None:
        return seconds

    # Fallback for "m:ss" or "h:mm:ss".
    duration = as_text(item.get("duration"))
    parts = duration.split(":")
    try:
        if len(parts) == 2:
            return float(parts[0]) * 60 + float(parts[1])
        if len(parts) == 3:
            return float(parts[0]) * 3600 + float(parts[1]) * 60 + float(parts[2])
    except ValueError:
        pass
    return None


def duration_matches(expected: Any, actual: Any, tolerance: float = 3.0) -> bool | None:
    expected_seconds = duration_seconds(expected)
    actual_seconds = duration_seconds(actual)
    if expected_seconds is None or actual_seconds is None:
        return None
    return abs(expected_seconds - actual_seconds) <= tolerance


def rank_candidate(
    item: dict[str, Any], tags: dict[str, Any]
) -> tuple[float, list[str]]:
    """Score a candidate conservatively; return score and mismatch notes."""
    notes: list[str] = []
    title_score = similarity(tags.get("title"), item.get("title"))
    artist_score = similarity(tags.get("artist"), result_artist(item))
    album_score = similarity(tags.get("album"), result_album(item))

    score = 0.60 * title_score + 0.30 * artist_score + 0.10 * album_score

    if title_score < 0.80:
        notes.append("title mismatch")
    if tags.get("artist") and artist_score < 0.75:
        notes.append("artist mismatch")

    duration_match = duration_matches(
        tags.get("duration_seconds"), duration_from_youtube(item)
    )
    if duration_match is False:
        notes.append("duration differs by more than 3 seconds")
        score -= 0.15

    return score, notes


def select_youtube_result(
    results: list[dict[str, Any]], tags: dict[str, Any]
) -> dict[str, Any]:
    songs = [
        item for item in results
        if isinstance(item, dict)
        and item.get("videoId")
        and item.get("resultType") in ("song", "video")
    ]
    ranked = sorted(
        (
            (rank_candidate(item, tags)[0], item)
            for item in songs
        ),
        key=lambda pair: pair[0],
        reverse=True,
    )

    candidates = [
        {
            "score": round(score, 3),
            "metadata": item,
        }
        for score, item in ranked
    ]

    if not ranked:
        return {"status": "not_found", "candidates": []}

    best_score, best = ranked[0]
    second_score = ranked[1][0] if len(ranked) > 1 else 0.0
    notes = rank_candidate(best, tags)[1]

    # Require a strong match and a clear lead; otherwise leave it for review.
    if best_score < 0.82 or best_score - second_score < 0.08:
        return {
            "status": "ambiguous",
            "candidates": candidates[:5],
            "review_notes": notes or ["top results are too close to choose safely"],
        }

    video_id = as_text(best.get("videoId"))
    return {
        "status": "matched",
        "video_id": video_id,
        "download_url": f"https://www.youtube.com/watch?v={video_id}",
        "metadata": best,
        "score": round(best_score, 3),
        "review_notes": notes,
    }


def lrc_items(payload: Any) -> list[dict[str, Any]]:
    """Accept common JSON list/wrapper response shapes."""
    if isinstance(payload, list):
        return [item for item in payload if isinstance(item, dict)]
    if isinstance(payload, dict):
        for key in ("results", "tracks", "items", "data"):
            value = payload.get(key)
            if isinstance(value, list):
                return [item for item in value if isinstance(item, dict)]
        if payload.get("isrc") or payload.get("lyricsUrl"):
            return [payload]
    return []


def lrc_candidate_score(
    item: dict[str, Any], tags: dict[str, Any]
) -> tuple[float, list[str]]:
    notes: list[str] = []
    isrc = as_text(item.get("isrc")).strip()
    source_isrc = as_text(tags.get("isrc")).strip()

    if source_isrc and isrc and source_isrc.casefold() == isrc.casefold():
        return 1.0, notes

    title_score = similarity(tags.get("title"), item.get("title"))
    artist_score = similarity(tags.get("artist"), item.get("artist"))
    score = 0.65 * title_score + 0.35 * artist_score

    if title_score < 0.80:
        notes.append("title mismatch")
    if tags.get("artist") and artist_score < 0.75:
        notes.append("artist mismatch")

    duration_match = duration_matches(
        tags.get("duration_seconds"), item.get("duration_seconds")
    )
    if duration_match is False:
        notes.append("duration differs by more than 3 seconds")
        score -= 0.15

    return score, notes


def lookup_lrc_red(
    session: requests.Session,
    gate: RequestGate,
    search_url: str,
    tags: dict[str, Any],
    api_base: str,
) -> dict[str, Any]:
    if not tags.get("title") and not tags.get("isrc"):
        return {"status": "skipped", "reason": "no title or ISRC tag"}

    params = {
        "title": tags.get("title", ""),
        "artist": tags.get("artist", ""),
        "album": tags.get("album", ""),
        "duration": tags.get("duration_seconds", ""),
        "isrc": tags.get("isrc", ""),
    }
    try:
        gate.wait()
        response = session.get(search_url, params=params, timeout=30)
        response.raise_for_status()
        candidates = lrc_items(response.json())
    except (requests.RequestException, ValueError) as exc:
        return {
            "status": "error",
            "error": f"{type(exc).__name__}: {exc}",
        }

    if not candidates:
        return {"status": "not_found", "candidates": []}

    ranked = sorted(
        (
            (lrc_candidate_score(item, tags)[0], item)
            for item in candidates
        ),
        key=lambda pair: pair[0],
        reverse=True,
    )
    best_score, best = ranked[0]
    second_score = ranked[1][0] if len(ranked) > 1 else 0.0
    notes = lrc_candidate_score(best, tags)[1]

    if best_score < 0.82 or best_score - second_score < 0.08:
        return {
            "status": "ambiguous",
            "candidates": [
                {"score": round(score, 3), "metadata": item}
                for score, item in ranked[:5]
            ],
            "review_notes": notes or ["top results are too close to choose safely"],
        }

    isrc = as_text(best.get("isrc")).strip()
    lyrics_url = as_text(best.get("lyricsUrl")).strip()
    if not lyrics_url and isrc:
        lyrics_url = urljoin(api_base.rstrip("/") + "/", f"s/{quote(isrc)}.ttml")

    result = {
        "status": "matched",
        "metadata": best,
        "score": round(best_score, 3),
        "review_notes": notes,
        "lyrics_url": lyrics_url,
        "lyrics": "",
    }

    if lyrics_url:
        try:
            gate.wait()
            lyric_response = session.get(lyrics_url, timeout=30)
            lyric_response.raise_for_status()
            result["lyrics"] = lyric_response.text
        except requests.RequestException as exc:
            result["lyrics_error"] = f"{type(exc).__name__}: {exc}"

    return result


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Look up tracks from a Step 1 indexed.json file."
    )
    parser.add_argument("index", type=Path, help="Input JSON created by music_indexer.py")
    parser.add_argument(
        "--output",
        type=Path,
        default=Path("lookups.json"),
        help="Output JSON file (default: ./lookups.json)",
    )
    parser.add_argument("--replace", action="store_true", help="Allow replacing output")
    parser.add_argument(
        "--lrc-search-url",
        default=os.environ.get("LRC_RED_SEARCH_URL", DEFAULT_LRC_SEARCH_URL),
        help="lrc.red search endpoint; can also be set with LRC_RED_SEARCH_URL",
    )
    parser.add_argument(
        "--delay",
        type=float,
        default=DELAY_SECONDS,
        help="Minimum seconds between requests to either service (default: 5)",
    )
    args = parser.parse_args()

    if args.output.exists() and not args.replace:
        print(f"{args.output} exists; use --replace to overwrite it.", file=sys.stderr)
        return 1

    try:
        document = json.loads(args.index.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        print(f"Could not read input index: {exc}", file=sys.stderr)
        return 1

    if not isinstance(document.get("tracks"), list):
        print("Input JSON has no 'tracks' list.", file=sys.stderr)
        return 1

    output_document = copy.deepcopy(document)
    gate = RequestGate(max(0.0, args.delay))
    yt = YTMusic()
    session = requests.Session()
    session.headers.update({"User-Agent": USER_AGENT, "Accept": "application/json"})

    for number, track in enumerate(output_document["tracks"], start=1):
        tags = track.get("original_tags", {})
        title = as_text(tags.get("title"))
        artist = as_text(tags.get("artist"))

        youtube_result: dict[str, Any]
        if not title and not artist:
            youtube_result = {
                "status": "skipped",
                "reason": "no title or artist tag",
                "candidates": [],
            }
        else:
            query = " ".join(
                value for value in (
                    title,
                    artist,
                    as_text(tags.get("album")),
                    as_text(tags.get("album_artist")),
                )
                if value
            )
            try:
                gate.wait()
                results = yt.search(query, filter="songs", limit=5)
                youtube_result = select_youtube_result(results, tags)
            except Exception as exc:
                youtube_result = {
                    "status": "error",
                    "error": f"{type(exc).__name__}: {exc}",
                }

        lrc_result = lookup_lrc_red(
            session=session,
            gate=gate,
            search_url=args.lrc_search_url,
            tags=tags,
            api_base="https://lrc.red",
        )

        # Store results separately; original_tags are not modified.
        track["lookups"] = {
            "youtube_music": youtube_result,
            "lrc_red": lrc_result,
        }
        print(f"[{number}/{len(output_document['tracks'])}] {title or '(untagged)'}")

    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(
        json.dumps(output_document, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    print(f"Wrote lookup results to {args.output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
