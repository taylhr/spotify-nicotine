"""Pure scoring logic: candidate filtering, match confidence, quality ranking.

Two separate scores per search result:

- ``match_confidence``: is this the right recording? (title/artist/duration).
  Gated by ``--min-confidence``.
- ``quality_score``: how desirable is this copy? (bitrate vs preference,
  uploader availability). Ranks candidates that passed the gate.
"""

import re
import unicodedata
from typing import Any, Dict, List, Optional, Set, Tuple

from rapidfuzz import fuzz

from spotify_nicotine.config import Config
from spotify_nicotine.models import (
    ATTR_BITRATE,
    ATTR_DURATION,
    ATTR_VBR,
    Candidate,
    LOSSLESS_EXTENSIONS,
    ParsedAttrs,
    Track,
)

# --- match confidence weights ---
W_TITLE = 0.55
W_ARTIST = 0.27
W_DURATION = 0.18
ALBUM_BONUS = 0.05
VERSION_PENALTY_PER_TOKEN = 0.25
VERSION_PENALTY_CAP = 0.5
# A same-title file with no trace of the artist anywhere in its path is the
# classic wrong-song trap (title-only fallback searches surface these).
# Genuine artist presence scores ~1.0 on partial_ratio; absence ~0.2-0.5.
ARTIST_EVIDENCE_THRESHOLD = 0.6
WEAK_ARTIST_PENALTY = 0.2
# Ranking tiers: an "excellent" match must never lose to a merely-eligible
# one on format/bitrate grounds (wrong song > no song is the failure mode).
EXCELLENT_CONFIDENCE = 0.9
DURATION_FULL_S = 3.0     # delta <= this -> full duration score
DURATION_ZERO_S = 15.0    # delta >= this -> zero duration score
DURATION_UNKNOWN_SCORE = 0.5
JUNK_FLOOR = 0.40         # below this a result is not even kept for review

# --- quality weights ---
Q_BITRATE = 0.55
Q_AVAILABILITY = 0.25
Q_SPEED = 0.12
Q_QUEUE = 0.08
TRANSCODE_SUSPECT_KBPS = 350   # lossless file smaller than this is suspect
INFERRED_BITRATE_DISCOUNT = 0.9
SPEED_FULL_BYTES_PER_S = 1_500_000

# Ranking prefers achieved bitrate (capped at --prefer-bitrate) BEFORE the
# format preference order; bands make near-equal bitrates (320 CBR vs ~317
# VBR) tie so the format order can break them.
BITRATE_BAND_KBPS = 32
UNKNOWN_BITRATE_KBPS = 128     # ranking assumption when bitrate is unknowable

TOP_K = 10

# Tokens that carry no identity information; stripped from BOTH sides.
NOISE_PHRASES = ("single version", "album version", "original mix", "radio version")
NOISE_TOKENS = frozenset({
    "remaster", "remastered", "remasterizado", "deluxe", "expanded", "edition",
    "anniversary", "reissue", "bonus", "explicit", "clean", "official", "audio",
    "hq", "hd", "stereo",
})
_YEAR_RE = re.compile(r"\b(19|20)\d{2}\b")

# Tokens that identify a DIFFERENT recording; never stripped, penalized when
# they appear on one side only.
VERSION_TOKEN_SYNONYMS = {"rmx": "remix", "instr": "instrumental"}
VERSION_TOKENS = frozenset({
    "live", "unplugged", "acoustic", "remix", "instrumental", "karaoke",
    "cover", "tribute", "demo", "session", "edit", "extended", "radio",
    "mono", "medley", "reprise", "slowed", "sped", "reverb", "nightcore",
    "8d", "mashup", "bootleg", "vip", "remake", "rework",
})

_FEAT_RE = re.compile(r"\b(?:feat|ft|featuring)\b.*$")
_TRACK_NUMBER_RE = re.compile(
    r"^\s*(?:(?:cd|disc)\s*\d{1,2}[\s._-]+)?(?:\d{1,2}[-.])?\d{1,3}[\s._-]+(?=\D)",
    re.IGNORECASE,
)
_PUNCT_RE = re.compile(r"[^\w\s]|_")
_WS_RE = re.compile(r"\s+")
_PAREN_RE = re.compile(r"[(\[][^()\[\]]*[)\]]")


def normalize(text: str) -> str:
    """Casefold, strip diacritics, punctuation -> space, collapse whitespace."""
    text = unicodedata.normalize("NFKD", text)
    text = "".join(ch for ch in text if not unicodedata.combining(ch))
    text = text.casefold()
    text = text.replace("&", " and ")
    text = text.replace("'", "").replace("’", "")
    text = _PUNCT_RE.sub(" ", text)
    return _WS_RE.sub(" ", text).strip()


def strip_track_number(basename: str) -> str:
    """Strip leading '05 - ', '1-05 ', 'cd2 03. ' style prefixes."""
    return _TRACK_NUMBER_RE.sub("", basename)


def _is_droppable_segment(segment: str) -> bool:
    """A parenthetical is dropped if it is a feat-clause or pure noise/years."""
    inner = normalize(segment)
    if not inner:
        return True
    tokens = inner.split()
    if tokens[0] in ("feat", "ft", "featuring", "with"):
        return True
    for phrase in NOISE_PHRASES:
        inner = inner.replace(phrase, " ")
    remaining = [
        tok for tok in _YEAR_RE.sub(" ", inner).split() if tok not in NOISE_TOKENS
    ]
    return not remaining


def core_title(text: str) -> str:
    """Reduce a title or filename to its identity-carrying tokens."""
    # Drop parenthetical segments that are feat-clauses or pure noise;
    # keep segments with version info ("(Live at Wembley)") intact.
    while True:
        replaced = _PAREN_RE.sub(
            lambda m: " " if _is_droppable_segment(m.group(0)[1:-1]) else m.group(0),
            text,
        )
        if replaced == text:
            break
        text = replaced

    text = normalize(text)
    text = _FEAT_RE.sub(" ", text)
    for phrase in NOISE_PHRASES:
        text = text.replace(phrase, " ")
    text = _YEAR_RE.sub(" ", text)
    tokens = [tok for tok in text.split() if tok not in NOISE_TOKENS]
    result = " ".join(tokens)
    return result if result else normalize(text)


def version_tokens(text: str) -> Set[str]:
    tokens = set(normalize(text).split())
    canonical = {VERSION_TOKEN_SYNONYMS.get(tok, tok) for tok in tokens}
    return canonical & VERSION_TOKENS


def split_virtual_path(virtual_path: str) -> Tuple[str, str]:
    """Split a Soulseek virtual path into (folder, basename-without-extension)."""
    name = virtual_path.replace("\\", "/").rsplit("/", 1)
    folder = name[0] if len(name) == 2 else ""
    basename = name[-1]
    if "." in basename:
        basename = basename.rsplit(".", 1)[0]
    return folder, basename


def parse_attrs(
    file_attributes: Optional[Dict[str, Any]],
    size: int,
    extension: str,
    spotify_duration_s: float,
) -> ParsedAttrs:
    """Extract bitrate/duration from Soulseek attributes, inferring when safe.

    Attribute keys may arrive as strings or ints depending on the JSON layer.
    """
    attrs: Dict[str, Any] = {}
    for key, value in (file_attributes or {}).items():
        attrs[str(key)] = value

    parsed = ParsedAttrs()

    raw_vbr = attrs.get(ATTR_VBR)
    if raw_vbr is not None:
        parsed.vbr = bool(raw_vbr)

    raw_bitrate = attrs.get(ATTR_BITRATE)
    if isinstance(raw_bitrate, (int, float)) and raw_bitrate > 0:
        parsed.bitrate_kbps = float(raw_bitrate)

    raw_duration = attrs.get(ATTR_DURATION)
    if isinstance(raw_duration, (int, float)) and raw_duration > 0:
        parsed.duration_s = float(raw_duration)

    is_lossless = extension in LOSSLESS_EXTENSIONS

    if parsed.duration_s is None and not is_lossless and parsed.bitrate_kbps and size > 0:
        parsed.duration_s = (size * 8) / (parsed.bitrate_kbps * 1000)
        parsed.duration_inferred = True

    if parsed.bitrate_kbps is None and size > 0:
        duration = parsed.duration_s or spotify_duration_s
        if duration and duration > 0:
            inferred = (size * 8) / (duration * 1000)
            if 32 <= inferred <= 4000:
                parsed.bitrate_kbps = inferred
                parsed.bitrate_inferred = True

    return parsed


def _duration_scores(
    candidate_duration_s: Optional[float], target_duration_s: float
) -> Tuple[float, float]:
    """Return (dur_score, dur_penalty)."""
    if candidate_duration_s is None or target_duration_s <= 0:
        return DURATION_UNKNOWN_SCORE, 0.0
    delta = abs(candidate_duration_s - target_duration_s)
    if delta <= DURATION_FULL_S:
        score = 1.0
    elif delta >= DURATION_ZERO_S:
        score = 0.0
    else:
        score = 1.0 - (delta - DURATION_FULL_S) / (DURATION_ZERO_S - DURATION_FULL_S)
    penalty = 0.0
    if delta > DURATION_ZERO_S:
        penalty = min(0.4, 0.2 + (delta - DURATION_ZERO_S) / 60.0 * 0.2)
    return score, penalty


def match_confidence(
    track: Track, folder: str, basename: str, attrs: ParsedAttrs
) -> float:
    stripped = strip_track_number(basename)
    target_core = core_title(track.name)
    norm_folder = normalize(folder)
    norm_basename = normalize(basename)

    # Score against both stripped and raw basename: stripping fixes
    # "05 - Title" but would mangle titles that legitimately start with a
    # number ("99 Problems"), so take the best of both.
    title_sim = 0.0
    for name_variant in {stripped, basename}:
        cand_core = core_title(name_variant)
        sim = (
            0.6 * fuzz.token_set_ratio(target_core, cand_core)
            + 0.4 * fuzz.token_sort_ratio(target_core, cand_core)
        ) / 100.0
        title_sim = max(title_sim, sim)

    artist_sim = 0.0
    for artist in track.artists:
        norm_artist = normalize(artist)
        if not norm_artist:
            continue
        best = max(
            fuzz.partial_ratio(norm_artist, norm_basename),
            fuzz.partial_ratio(norm_artist, norm_folder) if norm_folder else 0.0,
        )
        artist_sim = max(artist_sim, best / 100.0)

    dur_score, dur_penalty = _duration_scores(attrs.duration_s, track.duration_s)

    weak_artist_penalty = 0.0
    if track.artists and artist_sim < ARTIST_EVIDENCE_THRESHOLD:
        weak_artist_penalty = WEAK_ARTIST_PENALTY

    version_diff = version_tokens(stripped) ^ version_tokens(track.name)
    version_penalty = min(
        VERSION_PENALTY_CAP, VERSION_PENALTY_PER_TOKEN * len(version_diff)
    )

    album_bonus = 0.0
    if track.album and norm_folder:
        if fuzz.token_set_ratio(core_title(track.album), norm_folder) >= 85:
            album_bonus = ALBUM_BONUS

    confidence = (
        W_TITLE * title_sim
        + W_ARTIST * artist_sim
        + W_DURATION * dur_score
        + album_bonus
        - version_penalty
        - dur_penalty
        - weak_artist_penalty
    )
    return max(0.0, min(1.0, confidence))


def confidence_tier(confidence: float, min_confidence: float) -> int:
    """2 = excellent, 1 = auto-queue eligible, 0 = review-only."""
    if confidence >= EXCELLENT_CONFIDENCE:
        return 2
    if confidence >= min_confidence:
        return 1
    return 0


def _is_transcode_suspect(attrs: ParsedAttrs) -> bool:
    """A 'lossless' file whose effective bitrate is too low to be lossless."""
    return bool(attrs.bitrate_kbps and attrs.bitrate_kbps < TRANSCODE_SUSPECT_KBPS)


def bitrate_band(attrs: ParsedAttrs, extension: str, prefer_bitrate: int) -> int:
    """Quantized achieved-bitrate tier, capped at prefer_bitrate.

    Genuine lossless counts as a full band (it meets any lossy target), so a
    preferred-format file at the target bitrate still wins on format order.
    """
    if extension in LOSSLESS_EXTENSIONS and not _is_transcode_suspect(attrs):
        kbps = float(prefer_bitrate)
    elif attrs.bitrate_kbps:
        kbps = attrs.bitrate_kbps
    else:
        kbps = float(UNKNOWN_BITRATE_KBPS)
    return int(round(min(kbps, float(prefer_bitrate)) / BITRATE_BAND_KBPS))


def quality_score(attrs: ParsedAttrs, item: Dict[str, Any], cfg: Config) -> float:
    extension = str(item.get("extension", "")).lower()
    if extension in LOSSLESS_EXTENSIONS:
        if _is_transcode_suspect(attrs):
            bitrate_score = 0.2  # too small to be real lossless
        else:
            bitrate_score = 1.0
    elif attrs.bitrate_kbps:
        bitrate_score = 1.0 - min(
            1.0, abs(attrs.bitrate_kbps - cfg.prefer_bitrate) / float(cfg.prefer_bitrate)
        )
        if attrs.bitrate_inferred:
            bitrate_score *= INFERRED_BITRATE_DISCOUNT
    else:
        bitrate_score = 0.3  # unknown bitrate

    availability = 1.0 if item.get("free_upload_slots") else 0.0
    speed_score = min(1.0, (item.get("upload_speed") or 0) / SPEED_FULL_BYTES_PER_S)
    queue_score = max(0.0, 1.0 - (item.get("queue_position") or 0) / 100.0)

    return (
        Q_BITRATE * bitrate_score
        + Q_AVAILABILITY * availability
        + Q_SPEED * speed_score
        + Q_QUEUE * queue_score
    )


def _item_extension(item: Dict[str, Any]) -> str:
    extension = str(item.get("extension") or "").lower().lstrip(".")
    if not extension:
        path = str(item.get("file_path", ""))
        tail = path.replace("\\", "/").rsplit("/", 1)[-1]
        if "." in tail:
            extension = tail.rsplit(".", 1)[1].lower()
    return extension


def score_results(
    track: Track, items: List[Dict[str, Any]], cfg: Config
) -> List[Candidate]:
    """Filter, score, dedupe, and rank raw /search/results items.

    Ranking: bitrate band (capped at prefer_bitrate) first, then format
    preference order, then remaining quality, then confidence. Results whose
    known bitrate is under --min-bitrate are kept but flagged
    meets_min_bitrate=False: visible to the review picker, never auto-queued.
    """
    format_rank = {ext: i for i, ext in enumerate(cfg.formats)}
    seen: Set[Tuple[str, str]] = set()
    scored: List[Tuple[int, Candidate]] = []

    for item in items:
        username = item.get("username")
        file_path = item.get("file_path")
        if not username or not file_path:
            continue
        key = (username, file_path)
        if key in seen:
            continue
        seen.add(key)

        if item.get("is_private"):
            continue
        extension = _item_extension(item)
        if extension not in format_rank:
            continue

        size = int(item.get("size") or 0)
        attrs = parse_attrs(
            item.get("file_attributes"), size, extension, track.duration_s
        )
        meets_min_bitrate = not (
            cfg.min_bitrate
            and attrs.bitrate_kbps
            and attrs.bitrate_kbps < cfg.min_bitrate
        )

        folder, basename = split_virtual_path(file_path)
        confidence = match_confidence(track, folder, basename, attrs)
        if confidence < JUNK_FLOOR:
            continue

        item_with_ext = dict(item)
        item_with_ext["extension"] = extension
        scored.append(
            (
                bitrate_band(attrs, extension, cfg.prefer_bitrate),
                Candidate(
                    username=username,
                    virtual_path=file_path,
                    size=size,
                    extension=extension,
                    file_attributes=dict(item.get("file_attributes") or {}),
                    free_upload_slots=bool(item.get("free_upload_slots")),
                    queue_position=int(item.get("queue_position") or 0),
                    upload_speed=int(item.get("upload_speed") or 0),
                    confidence=confidence,
                    quality=quality_score(attrs, item_with_ext, cfg),
                    format_rank=format_rank[extension],
                    bitrate_kbps=attrs.bitrate_kbps,
                    bitrate_inferred=attrs.bitrate_inferred,
                    duration_s=attrs.duration_s,
                    meets_min_bitrate=meets_min_bitrate,
                ),
            )
        )

    scored.sort(
        key=lambda pair: (
            -confidence_tier(pair[1].confidence, cfg.min_confidence),
            -pair[0],
            pair[1].format_rank,
            -pair[1].quality,
            -pair[1].confidence,
        )
    )
    ranked = [candidate for _band, candidate in scored]
    kept = ranked[:TOP_K]

    # Sub-floor matches sort to the bottom, so truncation could hide the very
    # fact the notifier needs: a confident match that only exists below the
    # bitrate floor. When nothing auto-queueable survived, keep one witness.
    def _confident_below_floor(candidate: Candidate) -> bool:
        return (
            not candidate.meets_min_bitrate
            and candidate.confidence >= cfg.min_confidence
        )

    def _auto_queueable(candidate: Candidate) -> bool:
        return candidate.meets_min_bitrate and candidate.confidence >= cfg.min_confidence

    if (
        len(ranked) > TOP_K
        and not any(_auto_queueable(c) for c in kept)
        and not any(_confident_below_floor(c) for c in kept)
    ):
        overflow = [c for c in ranked[TOP_K:] if _confident_below_floor(c)]
        if overflow:
            kept[-1] = max(overflow, key=lambda c: c.confidence)

    return kept
