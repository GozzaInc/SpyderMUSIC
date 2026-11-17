# SpyderMUSIC Engine — extracted from Qt6 Alpha 20
# UI-independent playback / DJ / database core.

# =============================================================================
# SpyderMUSIC v3 – Vibe Precision Edition (ChromaDB + Last.fm)
# REF28: stereo VU pulse via proven MPV/FFmpeg astats + ametadata log path
# Backend: ChromaDB, Last.fm, ytmusicapi, MPV, yt-dlp
# =============================================================================

import base64
import configparser
import hashlib
import ctypes
import difflib
import json
import math
import os
import random
import re
import sqlite3
import subprocess
import sys
import threading
import time
import unicodedata
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager, closing
from io import BytesIO

import requests
from PIL import Image, ImageFilter, ImageEnhance
from ytmusicapi import YTMusic
import chromadb

from music_library import setup_library, scan_folder, add_audio_file, refresh_availability, list_audio
from offline_playback import OfflinePlaybackController
from playback_modes import AUTO_DJ, OFFLINE, VALID_MODES, policy_for
from playback_selection import from_legacy, legacy_mode
from playlist_manager import setup_playlists, create_playlist, rename_playlist, delete_playlist, list_playlists, list_playlist_tracks, add_track, remove_items, reorder_playlist, duplicate_playlist, export_m3u8, import_m3u
from pcm_decoder import PCMDecoder
from frequency_analyzer import FrequencyAnalyzer, VisualAnalyzer, FFT_SAMPLES
from libmpv_backend import LibMPV, LibMPVError

# =============================================================================
# CONSTANTS & VIBE PRESETS
# =============================================================================
UMLAUT_MAP = [('ö', 'o'), ('ä', 'a'), ('ü', 'u'), ('ß', 'ss'), ('é', 'e')]
COVER_BLUR_RADIUS = 4
COVER_BRIGHTNESS = 0.50
HISTORY_LIMIT = 10000
TAG_REFRESH_DAYS = 30
# The default embedding model is ChromaDB's local MiniLM. No paid cloud API required.

# Semantic Vector Mapping: Natural Language -> Precision Vector Descriptors
VIBE_PRESETS = {
    # Dynamics & Energy
    "High Energy": "high energy, driving rhythm, fast tempo, explosive",
    "Energetic": "energetic, upbeat tempo, dynamic rhythm",
    "Driving": "driving beat, forward momentum, propulsive rhythm, fast paced",
    "Party": "high energy party anthems, upbeat tempo, celebratory, loud hooks, good mood",
    "Anthemic": "epic stadium anthems, singalong chorus, massive hooks",
    "Weekend": "high energy party anthems, upbeat, weekend feeling, good mood",
    "In a good Mood": "in a good mood, positive energy, upbeat, catchy, feel good, fun",
    "In a bad Mood": "frustrated, tense, defiant, dark mood, emotional",
    
    # Grit & Attitude
    "Raw & Dirty": "raw, dirty, unpolished, gritty",
    "Heavy": "heavy, punchy bass, aggressive power",
    
    # Emotion & Depth
    "Melancholic": "melancholic, emotional, dramatic minor chords",
    "Dark": "dark, moody, ominous atmosphere",
    "Ballad": "emotional, heartfelt, slow tempo, soulful, expressive vocals",
    "Chill": "laid back, smooth, relaxed tempo",
    
    # Atmosphere & Theme
    "AOR Sunset": "melodic aor, 80s chorus, synth textures, smooth",
    "Spooky": "spooky, dark theatrical, horror atmosphere, eerie",
    "Halloween": "halloween, macabre theatrical, horror shock",
    "Acoustic": "acoustic, unplugged, stripped down"
}

def resolve_vibe_query(vibe: str) -> str:
    """Translates human-readable vibes into targeted vector descriptors or leaves raw input intact."""
    cleaned = vibe.strip()
    return VIBE_PRESETS.get(cleaned, cleaned)


# =============================================================================
# MUSICAL CONCEPTS: interpretable mood / energy / attitude signals
# The vector model handles free language; these lexicons only support reranking.
# =============================================================================
VIBE_CONCEPTS = {
    "energy": ("energetic", "high energy", "driving", "fast", "uptempo", "upbeat", "explosive",
               "powerful", "intense", "hard hitting", "punchy", "stadium", "party", "treibend", "energiegeladen"),
    "positive": ("good mood", "feel good", "happy", "uplifting", "optimistic", "positive",
                 "celebratory", "fun", "joyful", "cheerful", "sunny", "fröhlich", "gut drauf"),
    "melancholy": ("melancholic", "melancholy", "sad", "sorrow", "heartbreak", "depressive",
                   "emotional", "wistful", "bittersweet", "ballad", "melancholisch", "traurig"),
    "dark": ("dark", "ominous", "sinister", "spooky", "horror", "macabre", "eerie",
             "gothic", "atmospheric", "düster", "bedrohlich"),
    "raw": ("raw", "dirty", "gritty", "unpolished", "garage", "rough", "fuzzy", "sleaze",
            "dreckig", "rau", "rough edged"),
    "rebellious": ("defiant", "rebellious", "rebel", "angry", "attitude", "riot",
                   "punk", "aggressive", "frustrated", "wütend", "rebellisch", "trotzig"),
    "melodic": ("melodic", "aor", "harmonies", "catchy", "hook", "anthemic", "anthem",
                "singalong", "arena", "power pop", "harmonisch", "eingängig"),
    "relaxed": ("chill", "relaxed", "laid back", "smooth", "slow", "mellow", "soft",
                "ambient", "downtempo", "ruhig", "entspannt"),
    "acoustic": ("acoustic", "unplugged", "stripped down", "folk", "fingerstyle", "akustisch"),
    "retro": ("80s", "eighties", "70s", "seventies", "retro", "vintage", "synthwave"),
}
VIBE_OPPOSITES = {("positive", "melancholy"), ("energy", "relaxed"), ("positive", "dark")}

def _concept_hits(text: str) -> set:
    """Detect whole phrases; avoid matching e.g. 'raw' inside 'draw'."""
    text = unicodedata.normalize("NFKC", (text or "").lower())
    hits = set()
    for concept, phrases in VIBE_CONCEPTS.items():
        if any(re.search(r"(?<!\w)" + re.escape(phrase) + r"(?!\w)", text) for phrase in phrases):
            hits.add(concept)
    return hits


def _vibe_key(vibe: str) -> str:
    return " ".join((vibe or "").casefold().split())[:180]


def _clamp(value, low=0.0, high=1.0) -> float:
    return max(low, min(high, float(value)))


def _cosine_similarity(distance) -> float:
    # Only for v4 collections explicitly configured with cosine distance.
    if distance is None:
        return 0.32  # not in the top retrieved neighbors; neutral-low estimate
    return _clamp(1.0 - max(0.0, float(distance)))


def _tag_concept_alignment(meta: dict, target_vibe: str) -> tuple[float, str]:
    """Bonus evidence from *track* tags; artist tags get less confidence."""
    wanted = _concept_hits(target_vibe)
    if not wanted:
        return 0.5, "Semantics only"
    track_tags = (meta.get("track_tags") or "").strip()
    all_tags = track_tags or (meta.get("tags") or "")
    if not all_tags:
        return 0.5, "Tags unavailable"
    observed = _concept_hits(all_tags)
    if not observed:
        return 0.5, "No tag concepts"
    matches = len(wanted & observed) / len(wanted)
    conflicts = sum(1 for a,b in VIBE_OPPOSITES if (a in wanted and b in observed) or (b in wanted and a in observed))
    confidence = 1.0 if track_tags else 0.45
    evidence = _clamp(0.5 + 0.5 * matches - 0.28 * conflicts)
    return _clamp(0.5 + (evidence - 0.5) * confidence), f"Concepts: {matches:.0%}, conflicts: {conflicts}, {'track' if track_tags else 'artist'} tags"


def _spydertrack_judge_evidence(payload):
    """Return bounded, source-backed song descriptions; never infer vibe from feature names."""
    if not isinstance(payload, dict):
        return '', 0, 0
    features = payload.get('feature_summary') or {}
    if not isinstance(features, dict):
        return '', 0, 0
    allowed = {k: v for k, v in features.items()
               if k not in ('genre', 'production', 'productions') and isinstance(v, dict)}
    sentences = []
    source_count = 0
    for feature in allowed.values():
        try:
            source_count = max(source_count, int(feature.get('independent_sources') or 0))
        except (TypeError, ValueError):
            pass
        for evidence in feature.get('evidence', []):
            if not isinstance(evidence, dict):
                continue
            sentence = str(evidence.get('sentence') or '').strip()
            if sentence and sentence not in sentences:
                sentences.append(sentence[:400])
            if len(sentences) >= 12:
                break
    return ' '.join(sentences[:12]), len(allowed), source_count


def _spydertrack_judge_alignment(meta, target_vibe):
    """Small, evidence-only adjustment to vibe alignment; no genre/feature-count bonus."""
    text = (meta.get('_spydertrack_evidence') or '').strip()
    feature_count = int(meta.get('_spydertrack_feature_count') or 0)
    if not feature_count:
        return None, ''
    wanted = _concept_hits(target_vibe)
    observed = _concept_hits(text) if text else set()
    if not wanted or not observed:
        return None, f"SpyderTRACK: {feature_count} song features, no explicit vibe evidence"
    matches = len(wanted & observed) / len(wanted)
    conflicts = sum(1 for a, b in VIBE_OPPOSITES
                    if (a in wanted and b in observed) or (b in wanted and a in observed))
    evidence = _clamp(0.5 + 0.5 * matches - 0.28 * conflicts)
    return evidence, (f"SpyderTRACK: {feature_count} song features, "
                      f"vibe evidence {matches:.0%}, conflicts {conflicts}")


def _spydertrack_profile_alignment(meta, target_vibe):
    """Moderate, source-aware alignment from SpyderTRACK's explicit vibe profile."""
    profile = meta.get('_spydertrack_vibe_profile') or {}
    if not isinstance(profile, dict):
        return None, ''
    scores = profile.get('scores') or {}
    if not isinstance(scores, dict):
        return None, ''
    scores = {k: max(0.0, float(v)) for k, v in scores.items()
              if k in VIBE_CONCEPTS and isinstance(v, (int, float)) and not isinstance(v, bool) and v > 0}
    if not scores:
        return None, ''
    wanted = _concept_hits(target_vibe)
    if not wanted:
        return None, ''
    sources = profile.get('independent_sources') or {}
    if not isinstance(sources, dict):
        sources = {}
    # Multiple reviews increase confidence, not the magnitude of the preference.
    observed = {k: min(1.0, score / 2.0) * (0.7 + 0.3 * min(2, max(0, int(sources.get(k, 0) or 0))) / 2)
                for k, score in scores.items()}
    match = max((observed.get(k, 0.0) for k in wanted), default=0.0)
    conflict = max((observed.get(b, 0.0) for a, b in VIBE_OPPOSITES if a in wanted), default=0.0)
    conflict = max(conflict, max((observed.get(a, 0.0) for a, b in VIBE_OPPOSITES if b in wanted), default=0.0))
    if not match and not conflict:
        return None, ''  # Unrelated vibes do not count against a song.
    alignment = _clamp(0.5 + 0.5 * match - 0.28 * conflict)
    return alignment, f"SpyderTRACK profile: {', '.join(sorted(scores))}; match {match:.0%}, conflicts {conflict:.0%}"


def _normalized_affinity(score) -> float:
    # Doesn't allow historical scores in the hundreds to drown out the mood.
    return _clamp(0.5 + 0.5 * math.tanh((float(score) - 10.0) / 65.0))


def _vibe_score_preference(vibe_score: float, interactions: int) -> float:
    """Map a 0..100 per-vibe score to 0..1, softly until enough real evidence exists."""
    score = _clamp(float(vibe_score) / 100.0)
    confidence = min(1.0, max(0, int(interactions)) / 4.0)
    return 0.5 + (score - 0.5) * confidence

# =============================================================================
# PATHS
# =============================================================================
if getattr(sys, 'frozen', False):
    BASE_DIR = os.path.dirname(sys.executable)
else:
    BASE_DIR = os.path.dirname(os.path.abspath(__file__))

DATA_DIR = os.path.join(BASE_DIR, "data")
CHROMA_DIR = os.path.join(DATA_DIR, "chroma_db")

os.makedirs(DATA_DIR, exist_ok=True)
os.makedirs(CHROMA_DIR, exist_ok=True)

INI_FILE = os.path.join(DATA_DIR, "settings.ini")

def resource_path(relative_path):
    try:
        base_path = sys._MEIPASS
    except Exception:
        base_path = os.path.abspath(".")
    return os.path.join(base_path, relative_path)

icon_path = resource_path('icon.ico')


# =============================================================================
# CONFIGURATION
# =============================================================================
class Config:
    """
    Central application configuration.
    Values are loaded from / persisted to an INI file.
    Runtime-only fields are excluded from persistence.
    """

    _RUNTIME_FIELDS = frozenset({
        "RUNNING", "SONG_COUNTER", "CURRENT_ARTIST", "CURRENT_TITLE", "CURRENT_VIDEO_ID",
        "CURRENT_PENDING_SCORE", "ORIGINAL_DB_SCORE", "MANUAL_SCORE_GIVEN",
        "MANUAL_NEXT_TRACK", "PENALTY_APPLIED", "FORCE_SKIP", "TRACK_FAILED",
        "CURRENT_PLAY_VIBE", "CURRENT_PLAY_SOURCE", "LAST_DEQUEUED_SOURCE",
    })

    def __init__(self):
        self.AUDIO_DEVICE = "auto"

        # DJ Timers
        self.TRIGGER_SECONDS_BEFORE_END = 60
        self.CROSSFADE_SEC = 5
        self.TRACK_COOLDOWN_MINUTES = 240
        self.ARTIST_COOLDOWN_MINUTES = 90

        # Temperature / Vector Exploration Pool Factor
        self.TEMPERATURE = 0.3

        # Last.fm & Scout Settings
        self.LASTFM_API_KEY = ""
        self.WIKIDATA_CONTACT_EMAIL = ""
        self.SCOUT_ENABLED = True
        self.GENRE_PRESETS = (
            "Pop: Synth-Pop, Indie Pop, Dance-Pop, 80s Pop, New Wave, Pop-Rock, Electro-Pop | "
            "House: Deep House, Tech House, Progressive House, Vocal House, Acid House, Chicago House | "
            "Electronic: Synthwave, Darksynth, IDM, Ambient, Electro, Industrial, Techno | "
            "Hip Hop: Classic Hip Hop, 90s Boom Bap, Underground Rap, Conscious Rap, Instrumental Hip Hop | "
            "Rock: Classic Rock, NWOCR, NWOSS, Sleaze Rock, Hard Rock, Modern Rock, Alternative Rock, Grunge, Indie Rock, Blues Rock | "
            "Metal: Heavy Metal, NWOBHM, Thrash Metal, Power Metal, Symphonic Metal, Groove Metal"
        )
        self.SELECTED_GENRE_PRESET = "Custom"
        self.TARGET_GENRES = "Arena Rock, Modern Rock, Heavy Rock, Hard Rock, NWOCR, NWOSS, Sleaze Rock, Post-Grunge, Stoner Rock, Desert Rock, Dark Country"
        self.SELECTED_PROMPT_PREF = ""
        self.AI_PROMPT_LIST = (
            " , 70's, 80's, 90's, 2000's, 2010's, 2020's, BRAND NEW, Halloween, "
            "New Year, Party, Weekend, Chill, NWOSS, NWOCR, In a good Mood, In a bad Mood"
        )

        # Rating
        self.TOP_FAV_ARTISTS_COUNT = 100
        self.SCORE_THRESHOLD_PERCENT = 75
        self.PENALTY_THRESHOLD_PERCENT = 25
        self.POINTS_WIN = 10
        self.POINTS_PENALTY = -15
        # Explicit user queue choices are stronger intent signals than Judge selections.
        self.USER_QUEUE_GLOBAL_BONUS = 5
        self.USER_QUEUE_VIBE_WIN = 15
        self.USER_QUEUE_VIBE_PENALTY = -10

        # Databases
        self.DB_NAME = os.path.join(DATA_DIR, 'music.db')
        self.CHROMA_PATH = CHROMA_DIR

        # Runtime engine states
        self.SONG_COUNTER = 0
        self.CURRENT_ARTIST = ""
        self.CURRENT_TITLE = ""
        self.CURRENT_VIDEO_ID = ""
        self.CURRENT_PENDING_SCORE = 10
        self.ORIGINAL_DB_SCORE = 10
        self.MANUAL_SCORE_GIVEN = False
        self.MANUAL_NEXT_TRACK = ""
        self.STOP_AFTER_CURRENT = False
        self.FORCE_FADE_STOP = False
        self.REPEAT_TRACK = False
        self.RUNNING = True
        self.PENALTY_APPLIED = False
        self.FORCE_SKIP = False
        self.TRACK_FAILED = False 
        self.CURRENT_PLAY_VIBE = ""
        self.CURRENT_PLAY_SOURCE = "judge"
        self.LAST_DEQUEUED_SOURCE = ""

        self.DJ_ACTIVATED = False
        self.AUTO_DOWNLOAD = False
        # Persistent yt-dlp forecast; EMA updates after successful downloads.
        self.DOWNLOAD_AVG_SIZE_BYTES = 0.0
        self.DOWNLOAD_AVG_SPEED_BPS = 0.0
        self.DOWNLOAD_AVG_OVERHEAD_SEC = 0.0
        self.DOWNLOAD_SAMPLES = 0

        # Saved independently of the active theme.
        self.PLAYBACK_MODE = AUTO_DJ
        self.OFFLINE_SMART_SELECTION = False
        self.OFFLINE_ENRICHMENT = True

        self.load_from_ini()
        if self.PLAYBACK_MODE == 'hybrid':
            self.PLAYBACK_MODE = OFFLINE
        if self.PLAYBACK_MODE not in VALID_MODES:
            self.PLAYBACK_MODE = AUTO_DJ
        self.OFFLINE_SMART_SELECTION = False
        self.OFFLINE_ENRICHMENT = False
        # CHROMA_PATH is installation-local; never reuse it from old settings.
        self.CHROMA_PATH = CHROMA_DIR
        self.CURRENT_PENDING_SCORE = self.POINTS_WIN

    def load_from_ini(self):
        if not os.path.exists(INI_FILE):
            return
        parser = configparser.ConfigParser()
        parser.read(INI_FILE)
        if 'SETTINGS' not in parser:
            return
        section = parser['SETTINGS']
        for key in self.__dict__:
            if key == "CHROMA_PATH" or key not in section:
                continue
            try:
                current = getattr(self, key)
                if isinstance(current, bool):
                    setattr(self, key, section.getboolean(key))
                elif isinstance(current, int):
                    setattr(self, key, section.getint(key))
                elif isinstance(current, float):
                    setattr(self, key, section.getfloat(key))
                else:
                    raw = section.get(key)
                    if key == "DB_NAME" and not os.path.isabs(raw):
                        raw = os.path.normpath(os.path.join(BASE_DIR, raw))
                    setattr(self, key, raw)
            except Exception:
                pass

    def save_to_ini(self):
        with _INI_SAVE_LOCK:
            self._save_to_ini_locked()

    def _save_to_ini_locked(self):
        parser = configparser.ConfigParser()
        data = {}
        for key, value in self.__dict__.items():
            if key in self._RUNTIME_FIELDS or key == "CHROMA_PATH":
                continue
            save_value = value
            if key == "DB_NAME" and os.path.isabs(str(value)):
                try:
                    rel = os.path.relpath(str(value), BASE_DIR)
                    save_value = os.path.join(".", rel)
                except Exception:
                    pass
            data[key] = str(save_value)
        parser['SETTINGS'] = data
        with open(INI_FILE, 'w') as f:
            parser.write(f)


_INI_SAVE_LOCK = threading.RLock()
cfg = Config()

# One download worker, independent of the playback/Judge thread.
_AUDIO_PRELOAD_POOL = ThreadPoolExecutor(max_workers=1, thread_name_prefix="audio-preload")
_DOWNLOAD_STATS_LOCK = threading.RLock()


def _record_download_measurement(size_bytes: int, total_seconds: float, transfer_seconds: float):
    """Learn from successful real downloads; persist using the existing settings writer."""
    if size_bytes <= 0 or total_seconds <= 0 or transfer_seconds <= 0:
        return
    speed = size_bytes / transfer_seconds
    overhead = max(0.0, total_seconds - transfer_seconds)
    with _DOWNLOAD_STATS_LOCK:
        count = int(cfg.DOWNLOAD_SAMPLES)
        alpha = 1.0 / (count + 1) if count < 10 else 0.10
        for attr, sample in (
            ("DOWNLOAD_AVG_SIZE_BYTES", size_bytes),
            ("DOWNLOAD_AVG_SPEED_BPS", speed),
            ("DOWNLOAD_AVG_OVERHEAD_SEC", overhead),
        ):
            previous = float(getattr(cfg, attr))
            setattr(cfg, attr, sample if count == 0 or previous <= 0 else previous + alpha * (sample - previous))
        cfg.DOWNLOAD_SAMPLES = count + 1
        cfg.save_to_ini()


def _estimated_download_seconds():
    """Include yt-dlp setup overhead; use configured trigger until samples exist."""
    with _DOWNLOAD_STATS_LOCK:
        speed = float(cfg.DOWNLOAD_AVG_SPEED_BPS)
        size = float(cfg.DOWNLOAD_AVG_SIZE_BYTES)
        overhead = float(cfg.DOWNLOAD_AVG_OVERHEAD_SEC)
    if speed <= 0 or size <= 0:
        return float(getattr(cfg, 'TRIGGER_SECONDS_BEFORE_END', 60))
    return size / speed + overhead


def _choose_local_audio_fallback(exclude_key: str):
    """Judge-compatible scored local cache, respecting cooldowns; no remote fetch."""
    with db_connect() as conn:
        rows = conn.execute(
            "SELECT item_name, duration, genre FROM preferences "
            "WHERE item_type='track' AND local=1 AND duration>0 "
            "ORDER BY score DESC LIMIT 150"
        ).fetchall()
    for name, duration, genre in rows:
        if name.casefold() == exclude_key.casefold() or ' - ' not in name:
            continue
        artist, title = name.split(' - ', 1)
        if _resolved_track_is_on_cooldown(artist, name)[0]:
            continue
        path = os.path.join(DATA_DIR, 'cache', f'{_safe_audio_filename(name)}.webm')
        if os.path.isfile(path) and os.path.getsize(path) > 100000:
            return path, int(duration), artist, title, genre or 'Unknown'
    return None



# =============================================================================
# CHROMADB CLIENT & ENGINE (isolated vector collection per SQLite database)
# =============================================================================
# ChromaDB runs in a separate Python process (independent GIL).
# The process is created lazily to avoid Windows multiprocessing import recursion.
from chroma_process_service import ChromaService
chroma_client = None
_chroma_service = None


def _ensure_chroma_service():
    global _chroma_service, chroma_client
    if _chroma_service is None:
        _chroma_service = ChromaService(cfg.CHROMA_PATH)
        chroma_client = _chroma_service
    return _chroma_service

def shutdown_chroma_service():
    """Stop the owned Chroma worker explicitly; safe to call more than once."""
    global _chroma_service, chroma_client
    with _chroma_lock:
        service = _chroma_service
        _chroma_service = None
        chroma_client = None
    if service is not None:
        service.close()


chroma_collection = None
_chroma_active_db_path = ""
_chroma_lock = threading.RLock()
_chroma_rebuilding = set()
# IDs written/deleted while a background reconciliation is in progress must
# not be overwritten by a stale SQLite snapshot.
_chroma_live_changes = {}


def _db_absolute_path() -> str:
    path = cfg.DB_NAME
    if not os.path.isabs(path):
        path = os.path.join(BASE_DIR, path)
    return os.path.normcase(os.path.normpath(os.path.abspath(path)))


# SpyderTRACK: SQLite is the handoff; only this process writes ChromaDB.
_spydertrack_workers = set()
_spydertrack_worker_lock = threading.Lock()


def _spydertrack_payload_for(name):
    """Read a completed research report without changing SpyderTRACK's state."""
    try:
        with sqlite3.connect(_db_absolute_path(), timeout=10) as conn:
            cols = {r[1] for r in conn.execute("PRAGMA table_info(preferences)")}
            if not {'spydertrack_sync_status', 'spydertrack_data'} <= cols:
                return None
            row = conn.execute(
                "SELECT spydertrack_data FROM preferences WHERE item_name=? "
                "AND item_type='track' AND spydertrack_sync_status IN (1,2)", (name,)
            ).fetchone()
        return json.loads(row[0]) if row and row[0] else None
    except (sqlite3.Error, ValueError, TypeError):
        return None


def _spydertrack_add_to_record(doc, meta, payload):
    """Keep provenance in SQLite; index concise song-specific evidence in Chroma."""
    if not isinstance(payload, dict):
        return doc, meta
    features = payload.get('feature_summary') or {}
    if not isinstance(features, dict):
        return doc, meta
    features = {k: v for k, v in features.items()
                if k not in ('production', 'productions') and isinstance(v, dict)}
    profile = payload.get('vibe_profile') or {}
    scores = profile.get('scores') or {} if isinstance(profile, dict) else {}
    has_vibe = isinstance(scores, dict) and any(
        k in VIBE_CONCEPTS and isinstance(v, (int, float)) and v > 0
        for k, v in scores.items()
    )
    if not features and not has_vibe:
        return doc, meta
    feature_names = sorted(features)
    # A few bounded source sentences supply meaning beyond generic feature labels.
    snippets = []
    for name in feature_names:
        for ev in features[name].get('evidence', []):
            sentence = str(ev.get('sentence', '')).strip()
            if sentence and sentence not in snippets:
                snippets.append(sentence[:400])
            if len(snippets) >= 8:
                break
        if len(snippets) >= 8:
            break
    addition = 'Web research song features: ' + ', '.join(feature_names) + '.'
    if snippets:
        addition += ' Song descriptions: ' + ' '.join(snippets)
    if has_vibe:
        # The existing Chroma vector remains chiefly driven by tags and genre;
        # the Judge uses the explicit profile separately as a small rerank.
        meta['spydertrack_vibes'] = ', '.join(sorted(k for k, v in scores.items() if k in VIBE_CONCEPTS and isinstance(v, (int, float)) and v > 0))
    meta['spydertrack_features'] = ', '.join(feature_names)
    meta['spydertrack_sources'] = int(max([v.get('independent_sources', 0) or 0 for v in features.values()], default=0))
    return doc + ' ' + addition, meta


_PAYLOAD_UNSET = object()


def _chroma_record(item_name, item_type, score, last_played, genre,
                   video_id="", duration=0, year=0, description="",
                   listeners=0, tags="", similar="", track_tags="",
                   *, spydertrack_payload=_PAYLOAD_UNSET):
    """Keep per-track and fallback artist tags *separate* and inspectable."""
    name = (item_name or "").strip()
    genre = (genre or "Music").strip()
    tags = (tags or "").strip()
    track_tags = (track_tags or "").strip()
    parts = []
    if item_type == "track" and track_tags:
        parts.extend([f"Song mood, musical attributes, track tags: {track_tags}."] * 2)
    elif tags:
        parts.append(f"Music tags: {tags}.")
    if genre:
        parts.append(f"Genre: {genre}.")
    if tags and track_tags:
        parts.append(f"Artist context: {tags}.")
    parts.append(f"Artist and song: {name}.")
    if year:
        parts.append(f"Release year: {year}.")
    if similar:
        parts.append(f"Similar artists: {similar.strip()}.")
    meta = {
        "item_type": item_type,
        "score": int(score or 0),
        "last_played": float(last_played or 0),
        "genre": genre,
        "video_id": video_id or "",
        "duration": int(duration or 0),
        "year": int(year or 0),
        "listeners": int(listeners or 0),
        "tags": tags,
        "track_tags": track_tags,
        "description": (description or "").strip(),
    }
    doc = " ".join(parts)
    if item_type == 'track':
        payload = (_spydertrack_payload_for(name)
                   if spydertrack_payload is _PAYLOAD_UNSET else spydertrack_payload)
        doc, meta = _spydertrack_add_to_record(doc, meta, payload)
    return doc, meta


def _chroma_snapshot(collection):
    """Return persisted documents/metadata; reading never computes embeddings."""
    result = collection.get(include=['documents', 'metadatas'])
    ids = result.get('ids') or []
    docs = result.get('documents') or []
    metas = result.get('metadatas') or []
    return {
        ident: (docs[index] if index < len(docs) else None,
                metas[index] if index < len(metas) else None)
        for index, ident in enumerate(ids) if ident
    }


def _write_chroma_record(collection, ident, doc, meta):
    """Metadata-only updates must not recompute MiniLM embeddings."""
    current = collection.get(ids=[ident], include=['documents', 'metadatas'])
    if not (current.get('ids') or []):
        collection.upsert(ids=[ident], documents=[doc], metadatas=[meta])
        return 'new'
    old_doc = (current.get('documents') or [None])[0]
    old_meta = (current.get('metadatas') or [None])[0]
    if old_doc != doc:
        collection.upsert(ids=[ident], documents=[doc], metadatas=[meta])
        return 'embedded'
    if old_meta != meta:
        collection.update(ids=[ident], metadatas=[meta])
        return 'metadata'
    return 'unchanged'


def _sqlite_vector_rows(db_path):
    """Get an internally consistent SQLite snapshot, including SpyderTRACK data.

    A single SELECT avoids opening a new SQLite connection for every song.
    """
    fields = ('item_name, item_type, score, last_played, genre, video_id, '
              'duration, year, description, listeners, tags, similar, track_tags')
    with closing(sqlite3.connect(db_path, timeout=10)) as conn:
        columns = {row[1] for row in conn.execute('PRAGMA table_info(preferences)')}
        if {'spydertrack_data', 'spydertrack_sync_status'} <= columns:
            sql = (f'SELECT {fields}, '
                   'CASE WHEN spydertrack_sync_status IN (1,2) '
                   'THEN spydertrack_data ELSE NULL END FROM preferences')
        else:
            sql = f'SELECT {fields}, NULL FROM preferences'
        return conn.execute(sql).fetchall()


def _read_spydertrack_payload(raw):
    try:
        return json.loads(raw) if raw else None
    except (ValueError, TypeError):
        return None


def _reindex_chroma_from_sqlite(db_path, collection):
    """Reconcile only missing/changed vectors; no full CPU embedding pass.

    Existing documents are read from disk. Unmodified ones are never upserted,
    so a collection missing one record does not re-embed the whole library.
    """
    began = time.perf_counter()
    upserts, metadata_updates, removed, unchanged = [], [], [], 0
    written = 0
    try:
        rows = _sqlite_vector_rows(db_path)
        previous = _chroma_snapshot(collection)
        canonical = {}
        for row in rows:
            ident = (row[0] or '').strip()
            if ident:
                canonical[ident.casefold()] = ident
        # Remove orphaned/old-case entries, but do not touch any live mutation.
        stale = [ident for ident in previous
                 if canonical.get(ident.strip().casefold()) != ident]

        def flush():
            nonlocal written
            # Serialize only each bounded batch with direct SpyderTRACK writes.
            with _chroma_lock:
                changes = _chroma_live_changes.get(db_path, set())
                inserts = [x for x in upserts if x[0] not in changes]
                updates = [x for x in metadata_updates if x[0] not in changes]
                deletions = [x for x in removed if x not in changes]
                if inserts:
                    collection.upsert(ids=[x[0] for x in inserts],
                                      documents=[x[1] for x in inserts],
                                      metadatas=[x[2] for x in inserts])
                    written += len(inserts)
                if updates:
                    collection.update(ids=[x[0] for x in updates],
                                      metadatas=[x[2] for x in updates])
                    written += len(updates)
                if deletions:
                    collection.delete(ids=deletions)
            upserts.clear()
            metadata_updates.clear()
            removed.clear()

        for ident in stale:
            removed.append(ident)
            if len(removed) >= 48:
                flush()
        for row in rows:
            if not cfg.RUNNING or _db_absolute_path() != db_path:
                break
            ident = (row[0] or '').strip()
            if not ident:
                continue
            payload = _read_spydertrack_payload(row[-1])
            doc, meta = _chroma_record(*row[:-1], spydertrack_payload=payload)
            original = previous.get(ident)
            if original is None or original[0] != doc:
                upserts.append((ident, doc, meta))
            elif original[1] != meta:
                metadata_updates.append((ident, doc, meta))
            else:
                unchanged += 1
            if len(upserts) >= 48 or len(metadata_updates) >= 96:
                flush()
        flush()
        elapsed = time.perf_counter() - began
        log_to_gui(f'✅ Vector sync: {len(rows)} checked, {unchanged} unchanged, '
                   f'{written} updated/added, {len(stale)} stale IDs checked '
                   f'({elapsed:.1f}s).')
    except Exception as exc:
        log_to_gui(f'⚠️ Vector sync: {exc}')
    finally:
        with _chroma_lock:
            _chroma_rebuilding.discard(db_path)
            _chroma_live_changes.pop(db_path, None)


def _schedule_chroma_reconciliation(db_path, collection):
    with _chroma_lock:
        if db_path in _chroma_rebuilding:
            return
        _chroma_rebuilding.add(db_path)
        _chroma_live_changes[db_path] = set()
    start_thread(_reindex_chroma_from_sqlite, db_path, collection)


def initialize_chroma_for_active_db():
    global chroma_collection, _chroma_active_db_path
    try:
        _ensure_chroma_service()
    except Exception as exc:
        log_to_gui(f"⚠️ ChromaDB worker startup: {exc}")
        return
    db_path = _db_absolute_path()
    with _chroma_lock:
        if db_path == _chroma_active_db_path and chroma_collection is not None:
            return
        # Portable Collection-ID: relative SQLite-Position statt absolutem Installationspfad.
        # For SQLite outside the application directory, the external path remains the key.
        relative_db = os.path.relpath(db_path, BASE_DIR)
        if relative_db == ".." or relative_db.startswith(".." + os.sep):
            identity = "external:" + db_path.replace("\\\\", "/").casefold()
        else:
            identity = "local:" + relative_db.replace("\\\\", "/").casefold()
        digest = hashlib.sha256(identity.encode("utf-8")).hexdigest()[:16]
        name = f"spydermusic_v4_{digest}"
        try:
            collection = _chroma_service.open(name)
            chroma_collection = collection
            _chroma_active_db_path = db_path
            log_to_gui(f"🗄️ Vector library bound to {os.path.basename(db_path)} ({name})")
        except Exception as exc:
            chroma_collection = None
            _chroma_active_db_path = ""
            log_to_gui(f"⚠️ ChromaDB collection error: {exc}")
            return

    # Schedule all consistency checks in the worker thread, not on the GUI
    # startup path. No full re-embedding unless actual documents have changed.
    _schedule_chroma_reconciliation(db_path, collection)


def _spydertrack_sync_worker(db_path):
    """Drain status=1 research independently of playback and Judge."""
    try:
        while cfg.RUNNING and _db_absolute_path() == db_path:
            collection = chroma_collection
            if collection is None or _chroma_active_db_path != db_path or db_path in _chroma_rebuilding:
                time.sleep(5)
                continue
            try:
                with sqlite3.connect(db_path, timeout=10) as conn:
                    cols = {r[1] for r in conn.execute('PRAGMA table_info(preferences)')}
                    if not {'spydertrack_sync_status', 'spydertrack_data'} <= cols:
                        time.sleep(10)
                        continue
                    rows = conn.execute(
                        "SELECT item_name, item_type, score, last_played, genre, video_id, duration, "
                        "year, description, listeners, tags, similar, track_tags, spydertrack_data "
                        "FROM preferences WHERE item_type='track' AND spydertrack_sync_status=1 "
                        "ORDER BY item_name LIMIT 20"
                    ).fetchall()
                if not rows:
                    time.sleep(10)
                    continue
                transferred = 0
                for row in rows:
                    if not cfg.RUNNING or _db_absolute_path() != db_path or db_path in _chroma_rebuilding:
                        break
                    name, payload_text = row[0], row[-1]
                    try:
                        payload = json.loads(payload_text) if payload_text else None
                        features = payload.get('feature_summary', {}) if isinstance(payload, dict) else {}
                        profile = payload.get('vibe_profile') or {} if isinstance(payload, dict) else {}
                        scores = profile.get('scores') or {} if isinstance(profile, dict) else {}
                        has_features = isinstance(features, dict) and any(
                            k not in ('production', 'productions') and isinstance(v, dict)
                            for k, v in features.items()
                        )
                        has_vibe = isinstance(scores, dict) and any(
                            k in VIBE_CONCEPTS and isinstance(v, (int, float)) and v > 0
                            for k, v in scores.items()
                        )
                        if not has_features and not has_vibe:
                            # Empty results belong to SpyderTRACK's seven-day retry logic.
                            continue
                        with _chroma_lock:
                            doc, meta = _chroma_record(*row[:-1], spydertrack_payload=payload)
                            ident = name.strip()
                            _chroma_live_changes.get(db_path, set()).add(ident)
                            _write_chroma_record(collection, ident, doc, meta)
                        with sqlite3.connect(db_path, timeout=10) as conn:
                            conn.execute(
                                "UPDATE preferences SET spydertrack_sync_status=2 "
                                "WHERE item_name=? AND item_type='track' "
                                "AND spydertrack_sync_status=1 AND spydertrack_data=?",
                                (name, payload_text)
                            )
                        transferred += 1
                    except Exception as exc:
                        log_to_gui(f'⚠️ SpyderTRACK sync {name}: {exc}')
                if transferred:
                    log_to_gui(f'🔎 SpyderTRACK → ChromaDB: {transferred} songs synchronized.')
                if transferred == 0:
                    time.sleep(30)
            except Exception as exc:
                log_to_gui(f'⚠️ SpyderTRACK sync: {exc}')
                time.sleep(15)
    finally:
        with _spydertrack_worker_lock:
            _spydertrack_workers.discard(db_path)


def _start_spydertrack_sync(db_path):
    with _spydertrack_worker_lock:
        if db_path in _spydertrack_workers:
            return
        _spydertrack_workers.add(db_path)
    start_thread(_spydertrack_sync_worker, db_path)


def sync_track_to_chroma(
    item_name: str, item_type: str, score: int, last_played: float,
    genre: str, video_id: str = "", duration: int = 0,
    year: int = 0, description: str = "", listeners: int = 0,
    tags: str = "", similar: str = "", track_tags: str = ""
):
    collection = chroma_collection
    if collection is None:
        return
    try:
        doc, meta = _chroma_record(item_name, item_type, score, last_played,
                                   genre, video_id, duration, year, description,
                                   listeners, tags, similar, track_tags)
        with _chroma_lock:
            ident = item_name.strip()
            _chroma_live_changes.get(_chroma_active_db_path, set()).add(ident)
            _write_chroma_record(collection, ident, doc, meta)
    except Exception as exc:
        print(f"⚠️ ChromaDB Sync Error: {exc}")


def delete_from_chroma(item_name: str):
    collection = chroma_collection
    if collection is not None:
        try:
            with _chroma_lock:
                ident = item_name.strip()
                _chroma_live_changes.get(_chroma_active_db_path, set()).add(ident)
                collection.delete(ids=[ident])
        except Exception as exc:
            print(f"⚠️ ChromaDB delete failed: {exc}")

# =============================================================================
# YT MUSIC CLIENT
# =============================================================================
try:
    ytmusic = YTMusic()
except Exception:
    os.environ['LANGUAGE'] = 'en'
    ytmusic = YTMusic()


# =============================================================================
# MPV AUDIO ENGINE (DUAL DECK ARCHITECTURE)
# =============================================================================
class MPVEngine:
    """Two in-process libmpv decks; public M3/M4 interface is preserved."""

    def __init__(self):
        self.backend = LibMPV(BASE_DIR)
        self.decks = {
            name: {'player': self.backend.create_deck(name), 'peak_l': -90.0,
                   'peak_r': -90.0, 'meter_volume': 0.0, 'ready': False,
                   'target': None}
            for name in ('A', 'B')
        }
        self.active_deck = 'A'
        self.start_time = 0
        self.current_duration = 0
        self.is_paused = False
        self.pause_start_time = 0
        self.total_paused_time = 0
        self.playback_started = False
        self.load_start_time = 0
        self._watchdog_running = True  # GUI lifecycle compatibility
        self._generation = 0
        self._fade_generation = 0
        self._state_lock = threading.RLock()
        self.pcm = PCMDecoder(position=self.get_elapsed)
        self.frequency_analyzer = FrequencyAnalyzer()
        self.visual_analyzer = VisualAnalyzer()
        self._pcm_targets = {'A': None, 'B': None}
        threading.Thread(target=self._time_poller, daemon=True).start()

    def _player(self, name):
        return self.decks[name]['player']

    def _log_error(self, operation, exc):
        log_to_gui(f'[libmpv] {operation}: {exc}')

    def _time_poller(self):
        while self._watchdog_running:
            time.sleep(0.10)
            active = self.active_deck
            d = self.decks[active]
            player = d['player']
            position = player.number('time-pos')
            if position is not None and d['target'] is not None:
                if not self.playback_started:
                    self.playback_started = True
                    self.load_start_time = 0
                self.start_time = time.monotonic() - position
            duration = player.number('duration')
            if duration and duration > 0 and self.current_duration <= 0:
                self.current_duration = duration
            # Read filter metadata directly, no stdout parsing or console threads.
            for name, deck in self.decks.items():
                peaks = deck['player'].peaks()
                if peaks is None:
                    if deck['meter_volume'] <= 0:
                        deck['peak_l'] = deck['peak_r'] = -90.0
                    continue
                gain = (-90.0 if deck['meter_volume'] <= 0 else
                        20 * math.log10(max(0.00001, deck['meter_volume'] / 100)))
                def level(raw):
                    return max(-90.0, min(0.0, raw + gain)) if math.isfinite(raw) else -90.0
                deck['peak_l'], deck['peak_r'] = level(peaks[0]), level(peaks[1])
            # This is a local-file player. Missing/failed loads should not stall Auto DJ.
            if d['target'] is not None and not self.playback_started and self.load_start_time:
                if time.monotonic() - self.load_start_time > 12:
                    log_to_gui('[libmpv] Track did not start within 12 seconds; skipping.')
                    self.load_start_time = 0
                    cfg.FORCE_SKIP = True
                    cfg.TRACK_FAILED = True

    def get_meter_peaks(self, deck):
        d = self.decks.get(deck, {})
        return float(d.get('peak_l', -90)), float(d.get('peak_r', -90))

    def _reset_meter(self, deck):
        d = self.decks[deck]
        d['peak_l'] = d['peak_r'] = -90.0
        d['meter_volume'] = 0.0

    def _local_target(self, target):
        path = os.path.abspath(os.fspath(target))
        if not os.path.isfile(path):
            raise FileNotFoundError(f'Local audio file missing: {path}')
        return path

    def _set_volume(self, deck, value):
        self._player(deck).set('volume', float(value))
        self.decks[deck]['meter_volume'] = float(value)

    def _cancel_fades(self):
        with self._state_lock:
            self._fade_generation += 1
            return self._fade_generation

    def play(self, target, duration=0):
        try:
            target = self._local_target(target)
            self._cancel_fades()
            active = self.active_deck
            player = self._player(active)
            player.set('loop-file', 'no')  # Repeat One uses the other deck and normal crossfade
            player.set('pause', False)
            self._set_volume(active, 100)
            player.command('loadfile', target, 'replace')
            d = self.decks[active]
            d['target'], d['ready'] = target, False
            self._pcm_targets[active] = target
            self.pcm.start(target)
            self.playback_started = False
            self.load_start_time = time.monotonic()
            self.start_time = 0
            self.current_duration = duration
            self.total_paused_time = 0
            self.is_paused = False
            return True
        except Exception as exc:
            self._log_error('play', exc)
            return False

    def load_next(self, target):
        inactive = 'B' if self.active_deck == 'A' else 'A'
        d = self.decks[inactive]
        try:
            target = self._local_target(target)
            player = self._player(inactive)
            # A recycled deck must not report the previous track's path/duration
            # as readiness for the next one. Clear it before starting the load.
            d['ready'] = False
            d['target'] = None
            self._pcm_targets[inactive] = None
            player.command('stop')
            player.set('loop-file', 'no')
            player.set('pause', True)
            self._set_volume(inactive, 0)
            player.command('loadfile', target, 'replace')
            d['target'] = target
            self._pcm_targets[inactive] = target
            return True
        except Exception as exc:
            d['ready'] = False
            d['target'] = None
            self._log_error('load_next', exc)
            return False

    @staticmethod
    def _same_local_path(actual, expected):
        if not actual or not expected:
            return False
        # libmpv returns the file path, possibly with a file:// prefix.
        if actual.startswith('file://'):
            from urllib.parse import unquote, urlsplit
            parts = urlsplit(actual)
            actual = unquote(parts.path)
            if os.name == 'nt' and actual.startswith('/') and len(actual) > 2 and actual[2] == ':':
                actual = actual[1:]
        return os.path.normcase(os.path.normpath(actual)) == os.path.normcase(os.path.normpath(expected))

    def _wait_ready(self, deck, timeout=8.0):
        d = self.decks[deck]
        expected = d['target']
        if not expected:
            return False
        deadline = time.monotonic() + timeout
        player = d['player']
        while self._watchdog_running and time.monotonic() < deadline:
            actual = player.string('path')
            duration = player.number('duration')
            if self._same_local_path(actual, expected) and duration is not None and duration > 0:
                d['ready'] = True
                return True
            time.sleep(.05)
        self._log_error('deck readiness', f'Deck {deck}: expected {expected!r}, got {player.string("path")!r}, duration={player.number("duration")}')
        return False

    def _wait_playing(self, deck, timeout=2.0):
        """Do not silence the current deck unless the replacement advances."""
        player = self._player(deck)
        deadline = time.monotonic() + timeout
        previous = player.number('time-pos')
        while self._watchdog_running and time.monotonic() < deadline:
            current = player.number('time-pos')
            if (current is not None and previous is not None and
                    current > previous + 0.025 and player.flag('pause') is False):
                return True
            if current is not None:
                previous = current
            time.sleep(.05)
        return False

    def trigger_transition(self, new_duration, crossfade_sec=5):
        old = self.active_deck
        new = 'B' if old == 'A' else 'A'
        if not self.decks[new]['target'] or not self._wait_ready(new):
            self._log_error('transition', f'Deck {new} not ready; Deck {old} keeps playing')
            return False
        generation = self._cancel_fades()
        try:
            self._set_volume(new, 0)
            self._player(new).set('pause', False)
            if not self._wait_playing(new):
                self._player(new).set('pause', True)
                self._log_error('transition', f'Deck {new} did not start; Deck {old} keeps playing')
                return False
        except Exception as exc:
            self._log_error('transition start', exc)
            return False
        self.active_deck = new
        self.current_duration = new_duration
        if self._pcm_targets[new]:
            self.pcm.start(self._pcm_targets[new])
        else:
            self.pcm.stop()
        self.start_time = 0
        self.playback_started = False
        self.load_start_time = time.monotonic()
        self.total_paused_time = 0
        self.is_paused = False

        def fade_worker():
            seconds = max(0.0, float(crossfade_sec))
            steps = max(1, int(seconds * 20))
            start = time.monotonic()
            for i in range(steps + 1):
                if not self._watchdog_running or generation != self._fade_generation:
                    return
                progress = i / steps
                try:
                    self._set_volume(old, 100 * math.cos(progress * math.pi / 2))
                    self._set_volume(new, 100 * math.sin(progress * math.pi / 2))
                except Exception as exc:
                    self._log_error('crossfade', exc)
                    return
                if i < steps:
                    time.sleep(max(0, start + seconds * (i + 1) / steps - time.monotonic()))
            if generation == self._fade_generation:
                try:
                    self._player(old).command('stop')
                    self.decks[old]['target'] = None
                    self.decks[old]['ready'] = False
                    self._pcm_targets[old] = None
                    self._reset_meter(old)
                except Exception as exc:
                    self._log_error('old deck cleanup', exc)
        threading.Thread(target=fade_worker, daemon=True).start()
        return True

    def get_elapsed(self) -> float:
        if not self.playback_started:
            return 0.0
        position = self._player(self.active_deck).number('time-pos')
        return max(0.0, position) if position is not None else 0.0

    def is_playing(self) -> bool:
        player = self._player(self.active_deck)
        return bool(self.playback_started and player.flag('idle-active') is False)

    def fade_out_and_stop(self, crossfade_sec=5, on_complete=None):
        self.fade_stop_active = True
        active = self.active_deck
        generation = self._cancel_fades()
        def worker():
            seconds = max(0.0, float(crossfade_sec))
            steps = max(1, int(seconds * 20))
            start = time.monotonic()
            for i in range(steps + 1):
                if generation != self._fade_generation or not self._watchdog_running:
                    return
                try:
                    self._set_volume(active, 100 * math.cos((i / steps) * math.pi / 2))
                except Exception as exc:
                    self._log_error('fade stop', exc)
                    return
                if i < steps:
                    time.sleep(max(0, start + seconds * (i + 1) / steps - time.monotonic()))
            if generation != self._fade_generation:
                return
            self.stop()
            if on_complete is not None:
                try:
                    on_complete()
                except Exception as exc:
                    self._log_error('fade completion callback', exc)
            else:
                cfg.FORCE_FADE_STOP = True
        threading.Thread(target=worker, daemon=True).start()

    def get_frequency_bands(self):
        return self.frequency_analyzer.update(self.get_pcm_window(FFT_SAMPLES))

    def get_visual_audio(self):
        return self.visual_analyzer.update(self.get_pcm_window(FFT_SAMPLES))

    def get_pcm_window(self, samples=2048):
        if self.is_paused or not self.playback_started:
            return None
        return self.pcm.window(self.get_elapsed(), samples)

    def stop(self):
        self.fade_stop_active = False
        self._cancel_fades()
        self.pcm.stop()
        for name in ('A', 'B'):
            try:
                self._player(name).command('stop')
                self.decks[name]['target'] = None
                self._reset_meter(name)
            except Exception as exc:
                self._log_error('stop', exc)
        self.is_paused = False
        self.playback_started = False
        self.start_time = 0
        self.current_duration = 0
        self.load_start_time = 0

    def toggle_pause(self):
        try:
            new_state = not self.is_paused
            self._player(self.active_deck).set('pause', new_state)
            self.is_paused = new_state
        except Exception as exc:
            self._log_error('pause', exc)

    def seek_absolute(self, seconds: float):
        try:
            self._player(self.active_deck).command('seek', str(max(0.0, float(seconds))), 'absolute', 'exact')
            self.start_time = time.monotonic() - seconds
        except Exception as exc:
            self._log_error('seek', exc)

    def set_loop(self, state: bool):
        try:
            # Native looping would bypass deck transitions and stall PCM/FFT.
            for deck in ('A', 'B'):
                self._player(deck).set('loop-file', 'no')
        except Exception as exc:
            self._log_error('repeat one', exc)

    def kill_all(self):
        if not self._watchdog_running:
            return
        self._watchdog_running = False
        self._cancel_fades()
        self.pcm.stop()
        for d in self.decks.values():
            d['player'].close()

mpv_player = MPVEngine()


# =============================================================================
# UTILITIES
# =============================================================================
def ensure_single_instance():
    mutex_name = "Global\\SpyderMUSIC_Engine_Lock"
    mutex = ctypes.windll.kernel32.CreateMutexW(None, False, mutex_name)
    if ctypes.windll.kernel32.GetLastError() == 183:
        print("🛑 SpyderMUSIC is already running! Aborting new instance.")
        sys.exit(0)
    return mutex


@contextmanager
def db_connect():
    path = cfg.DB_NAME
    if not os.path.isabs(path):
        path = os.path.normpath(os.path.join(BASE_DIR, path))
    conn = sqlite3.connect(path)
    try:
        yield conn
        conn.commit()
    except Exception as exc:
        conn.rollback()
        raise exc
    finally:
        conn.close()


def start_thread(target, *args):
    threading.Thread(target=target, args=args, daemon=True).start()


def dedupe_case_insensitive(items: list) -> list:
    seen: set = set()
    result = []
    for item in items:
        cleaned = item.strip()
        key = cleaned.lower()
        if key and len(key) > 1 and key not in seen:
            seen.add(key)
            result.append(cleaned)
    return result


def get_resource_path(filename: str) -> str:
    return os.path.join(BASE_DIR, filename)


# =============================================================================
# GUI REFERENCES
# =============================================================================
class _GUI:
    root = None
    log_box = None
    ai_box = None
    scout_status = None
    scout_detail = None
    lbl_track = None
    lbl_time = None
    lbl_current_score = None
    lbl_cover = None
    seek_bar = None
    hist_box = None
    queue_box = None
    list_top = None
    cb_search = None
    bg_canvas = None
    db_var = None
    btn_ai_skip = None
    btn_stop_after = None
    btn_repeat = None
    btn_ban = None
    top_x_label_var = None
    last_user_interaction: float = 0
    e_quick_top = None
    e_expert_top = None
    blacklist_tree = None
    vibe_tree = None
    vibe_filter_label = None
    vibe_search_var = None
    vibe_current_only = False
    blacklist_search_var = None
    vu_meter_a = None
    vu_meter_b = None


gui = _GUI()


# Removed unused legacy Tk component: StereoVUMeter


def update_vu_meters(peaks_a=None, peaks_b=None):
    if gui.root is None or not getattr(cfg, 'RUNNING', False):
        return
    try:
        if gui.vu_meter_a is not None:
            gui.vu_meter_a.update_levels(*(peaks_a if peaks_a is not None else mpv_player.get_meter_peaks('A')))
        if gui.vu_meter_b is not None:
            gui.vu_meter_b.update_levels(*(peaks_b if peaks_b is not None else mpv_player.get_meter_peaks('B')))
    except Exception:
        pass
    # V4: scheduled by a persistent Qt timer in spyder_gui.py.


def log_to_gui(msg: str):
    print(msg)
    if gui.log_box is not None and gui.root is not None and getattr(cfg, 'RUNNING', False):
        try:
            gui.root.after(0, lambda m=msg: (
                gui.log_box.insert("end", f"{m}\n"),
                gui.log_box.see("end"),
            ))
        except Exception:
            pass


def ai_log(msg: str):
    print(f"AI-Log: {msg}")
    if gui.ai_box is not None and gui.root is not None and getattr(cfg, 'RUNNING', False):
        try:
            gui.root.after(0, lambda m=msg: (
                gui.ai_box.insert("end", f"{m}\n"),
                gui.ai_box.see("end"),
            ))
        except Exception:
            pass


def scout_display(discovery: str, detail: str = ""):
    print(f"Scout: {discovery} | {detail}" if detail else f"Scout: {discovery}")
    if gui.root is None or not getattr(cfg, 'RUNNING', False):
        return

    def update():
        if gui.scout_status is not None and gui.scout_status.winfo_exists():
            gui.scout_status.configure(text=discovery)
        if gui.scout_detail is not None and gui.scout_detail.winfo_exists():
            gui.scout_detail.configure(text=detail)

    try:
        gui.root.after(0, update)
    except Exception:
        pass


def _do_clear_gui_logs(clear_ai: bool, clear_main: bool):
    try:
        if clear_ai and gui.ai_box is not None:
            gui.ai_box.delete('1.0', "end")
        if clear_main and gui.log_box is not None:
            gui.log_box.delete('1.0', "end")
            log_to_gui("🧹 SpyderCore: Queue buffer cleared…")
    except Exception:
        pass


def clear_gui_logs(ai: bool = True, main: bool = False):
    if gui.root is not None and getattr(cfg, 'RUNNING', False):
        try:
            gui.root.after(0, lambda: _do_clear_gui_logs(ai, main))
        except Exception:
            pass


# =============================================================================
# DATABASE SETUP
# =============================================================================
def setup_database():
    """Idempotent schema migration including judge_rotation."""
    with db_connect() as conn:
        conn.execute(
            'CREATE TABLE IF NOT EXISTS preferences ('
            'item_name TEXT PRIMARY KEY COLLATE NOCASE, '
            'item_type TEXT, score INTEGER DEFAULT 0, last_played REAL DEFAULT 0, '
            'genre TEXT DEFAULT "", video_id TEXT DEFAULT "", duration INTEGER DEFAULT 0, '
            'local INTEGER DEFAULT 0, year INTEGER DEFAULT 0, '
            'description TEXT DEFAULT "", listeners INTEGER DEFAULT 0, '
            'tags TEXT DEFAULT "", similar TEXT DEFAULT "", image_blob BLOB, '
            'track_tags TEXT DEFAULT "", track_tag_checked_at REAL DEFAULT 0, scout_checked INTEGER NOT NULL DEFAULT 0)'
        )
        current = {row[1] for row in conn.execute('PRAGMA table_info(preferences)')}
        for name, ddl in {
            'tags': 'TEXT DEFAULT ""', 'similar': 'TEXT DEFAULT ""',
            'track_tags': 'TEXT DEFAULT ""', 'track_tag_checked_at': 'REAL DEFAULT 0', 'scout_checked': 'INTEGER NOT NULL DEFAULT 0',
            'year': 'INTEGER DEFAULT 0', 'description': 'TEXT DEFAULT ""',
            'listeners': 'INTEGER DEFAULT 0', 'image_blob': 'BLOB',
            'local': 'INTEGER DEFAULT 0', 'video_id': 'TEXT DEFAULT ""',
            'duration': 'INTEGER DEFAULT 0',
        }.items():
            if name not in current:
                conn.execute(f'ALTER TABLE preferences ADD COLUMN {name} {ddl}')
        if 'scout_checked' not in current:
            conn.execute("UPDATE preferences SET scout_checked=1 WHERE item_type='track' AND COALESCE(track_tag_checked_at,0)>0")
        conn.execute(
            'CREATE TABLE IF NOT EXISTS queue ('
            'item TEXT, timestamp REAL, source TEXT DEFAULT "user_queue")'
        )
        conn.execute(
            'CREATE TABLE IF NOT EXISTS vibe_feedback ('
            'track_key TEXT COLLATE NOCASE NOT NULL, '
            'vibe_key TEXT COLLATE NOCASE NOT NULL, '
            'vibe_score REAL NOT NULL DEFAULT 50, '
            'interactions INTEGER NOT NULL DEFAULT 0, '
            'last_updated REAL DEFAULT 0, '
            'PRIMARY KEY (track_key, vibe_key))'
        )
        # Dedicated judge rotation counter (Section 8)
        conn.execute(
            "CREATE TABLE IF NOT EXISTS judge_rotation ("
            "track_key TEXT PRIMARY KEY COLLATE NOCASE, "
            "judge_plays INTEGER NOT NULL DEFAULT 0)"
        )
        # Persistently learned artist aliases, shared by all tracks in this database.
        conn.execute(
            "CREATE TABLE IF NOT EXISTS artist_aliases ("
            "original_name TEXT PRIMARY KEY COLLATE NOCASE, "
            "resolved_name TEXT NOT NULL, "
            "resolved_at REAL NOT NULL)"
        )
        # Local library: additive schema, independent of ChromaDB and preferences.
        setup_library(conn)
        setup_playlists(conn)
        # Dedicated blacklist table for permanent bans
        conn.execute(
            'CREATE TABLE IF NOT EXISTS blacklist ('
            'item_name TEXT PRIMARY KEY COLLATE NOCASE, '
            'item_type TEXT, '
            'created_at REAL)'
        )
    initialize_chroma_for_active_db()
    _start_spydertrack_sync(_db_absolute_path())

# Removed unused legacy Tk component: create_new_db

# =============================================================================
# PLAYLIST TRAINING
# =============================================================================
# Removed unused legacy Tk component: fetch_playlist_data


# Removed unused legacy Tk component: save_training_data


# =============================================================================
# SCORING, BLACKLIST & ROTATION HELPERS
# =============================================================================
def record_judge_rotation(track_key: str, source: str):
    """Persistently counts only actual Judge plays (Section 8)."""
    if source != "judge" or not track_key or " - " not in track_key:
        return
    try:
        with db_connect() as conn:
            conn.execute(
                "INSERT INTO judge_rotation(track_key, judge_plays) VALUES (?, 1) "
                "ON CONFLICT(track_key) DO UPDATE SET judge_plays = judge_plays + 1",
                (track_key.strip(),),
            )
    except Exception as exc:
        print(f"⚠️ Judge rotation counter skipped: {exc}")


def update_score(name: str, item_type: str, points: int):
    if not name or name == "Unknown": return
    now = time.time()
    with db_connect() as conn:
        conn.execute(
            "INSERT INTO preferences (item_name, item_type, score, last_played, genre) "
            "VALUES (?, ?, ?, ?, 'Unknown') "
            "ON CONFLICT(item_name) DO UPDATE SET "
            "score = score + excluded.score, last_played = excluded.last_played",
            (name.strip(), item_type, points, now),
        )
        row = conn.execute(
            "SELECT score, last_played, genre, video_id, duration, year, description, listeners, tags, similar, track_tags FROM preferences WHERE item_name = ?", 
            (name.strip(),)
        ).fetchone()
        if row:
            sync_track_to_chroma(
                name.strip(), item_type, row[0], row[1], row[2], row[3], row[4], 
                year=row[5] or 0, description=row[6] or "", listeners=row[7] or 0,
                tags=row[8] or "", similar=row[9] or "", track_tags=row[10] or ""
            )


def get_blacklist_sets() -> tuple[set, set]:
    """Return permanent track/artist bans as casefolded sets."""
    try:
        with db_connect() as conn:
            rows = conn.execute("SELECT item_name, item_type FROM blacklist").fetchall()
        banned_tracks = {name.strip().casefold() for name, kind in rows if kind == "track" and name}
        banned_artists = {name.strip().casefold() for name, kind in rows if kind == "artist" and name}
        return banned_tracks, banned_artists
    except Exception:
        return set(), set()


def refresh_blacklist_ui():
    tree = getattr(gui, "blacklist_tree", None)
    if tree is None:
        return
    try:
        for item in tree.get_children():
            tree.delete(item)
        with db_connect() as conn:
            rows = conn.execute(
                "SELECT item_name, item_type, created_at FROM blacklist "
                "ORDER BY item_type, item_name COLLATE NOCASE"
            ).fetchall()
        query = ""
        if getattr(gui, "blacklist_search_var", None) is not None:
            query = gui.blacklist_search_var.get().strip().casefold()
        for name, kind, created_at in rows:
            stamp = time.strftime("%Y-%m-%d %H:%M", time.localtime(created_at or 0)) if created_at else ""
            values = ("Artist" if kind == "artist" else "Track", name, stamp)
            if query and query not in " ".join(str(v) for v in values).casefold():
                continue
            tree.insert("", "end", values=values)
        if hasattr(tree, "_apply_sort"):
            tree._apply_sort()
    except Exception as exc:
        log_to_gui(f"⚠️ Blacklist UI refresh failed: {exc}")


def unban_selected_items():
    tree = getattr(gui, "blacklist_tree", None)
    if tree is None:
        return
    selected = tree.selection()
    if not selected:
        return
    removed = []
    with db_connect() as conn:
        for item in selected:
            values = tree.item(item, "values")
            if len(values) < 2:
                continue
            kind_label, name = values[0], values[1]
            kind = "artist" if kind_label == "Artist" else "track"
            conn.execute("DELETE FROM blacklist WHERE item_name=? COLLATE NOCASE AND item_type=?", (name, kind))
            removed.append(name)
    refresh_blacklist_ui()
    if removed:
        log_to_gui(f"✅ [Blacklist] Unbanned: {', '.join(removed)}")


# Removed unused legacy Tk component: build_blacklist_tab


def ban_current_track():
    track_key = f"{cfg.CURRENT_ARTIST} - {cfg.CURRENT_TITLE}".strip()
    if not track_key or track_key in ("-", "? - ?"):
        return
    with db_connect() as conn:
        conn.execute(
            "INSERT OR IGNORE INTO blacklist (item_name, item_type, created_at) VALUES (?, 'track', ?)",
            (track_key, time.time())
        )
    delete_from_chroma(track_key)
    log_to_gui(f"🚫 [Blacklist] Track permanently banned: '{track_key}'")
    if gui.root is not None:
        gui.root.after(0, refresh_blacklist_ui)
    cfg.FORCE_SKIP = True


def ban_current_artist():
    artist = (cfg.CURRENT_ARTIST or "").strip()
    if not artist or artist in ("?", "Unknown"):
        return
    with db_connect() as conn:
        conn.execute(
            "INSERT OR IGNORE INTO blacklist (item_name, item_type, created_at) VALUES (?, 'artist', ?)",
            (artist, time.time())
        )
    delete_from_chroma(artist)
    log_to_gui(f"⛔ [Blacklist] Artist permanently banned: '{artist}'")
    if gui.root is not None:
        gui.root.after(0, refresh_blacklist_ui)
    cfg.FORCE_SKIP = True


def log_playback_and_get_score(artist: str, track_key: str) -> int:
    now = time.time()
    score = 10
    with db_connect() as conn:
        conn.executemany(
            "INSERT INTO preferences (item_name, item_type, score, last_played, genre) "
            "VALUES (?, ?, 10, ?, 'Unknown') "
            "ON CONFLICT(item_name) DO UPDATE SET last_played = excluded.last_played",
            [(artist.strip(), 'artist', now), (track_key.strip(), 'track', now)]
        )
        row = conn.execute(
            "SELECT score, genre, video_id, duration, year, description, listeners, tags, similar, track_tags FROM preferences WHERE item_name = ?", 
            (track_key.strip(),)
        ).fetchone()
        if row: 
            score = row[0]
            sync_track_to_chroma(
                track_key.strip(), "track", score, now, row[1], row[2], row[3], 
                year=row[4] or 0, description=row[5] or "", listeners=row[6] or 0,
                tags=row[7] or "", similar=row[8] or "", track_tags=row[9] or ""
            )
    return score


def get_db_suggestions(search_term: str) -> list:
    if not search_term:
        return []
    try:
        with db_connect() as conn:
            return [
                row[0] for row in conn.execute(
                    "SELECT item_name FROM preferences "
                    "WHERE item_type = 'track' AND item_name LIKE ? COLLATE NOCASE "
                    "ORDER BY item_name COLLATE NOCASE",
                    (f'%{search_term}%',),
                ).fetchall()
            ]
    except Exception:
        return []


def _get_track_or_artist_genre(track_name: str, artist_name: str) -> str:
    try:
        with db_connect() as conn:
            row = conn.execute(
                "SELECT genre FROM preferences WHERE item_name = ? AND item_type = 'track' COLLATE NOCASE",
                (track_name.strip(),),
            ).fetchone()
            if row and row[0] and row[0].strip().lower() not in ("unknown", "", "music"):
                return row[0].strip()

            row = conn.execute(
                "SELECT genre FROM preferences WHERE item_name = ? AND item_type = 'artist' COLLATE NOCASE",
                (artist_name.strip(),),
            ).fetchone()
            if row and row[0] and row[0].strip().lower() not in ("unknown", "", "music"):
                return row[0].strip()
        return "Unknown"
    except Exception:
        return "Unknown"


# =============================================================================
# LAST.FM PER-SONG TAG ENRICHMENT
# =============================================================================
def _fetch_track_top_tags(artist: str, title: str) -> tuple[str, bool]:
    if not cfg.LASTFM_API_KEY.strip():
        return "", False
    try:
        response = requests.get(
            "https://ws.audioscrobbler.com/2.0/",
            params={"method": "track.getTopTags", "api_key": cfg.LASTFM_API_KEY,
                    "artist": artist, "track": title, "autocorrect": "1", "format": "json"},
            timeout=8,
        )
        response.raise_for_status()
        data = response.json()
        if "error" in data:
            return "", data.get("error") in (6,)
        tag_rows = data.get("toptags", {}).get("tag", [])
        if isinstance(tag_rows, dict):
            tag_rows = [tag_rows]
        result = []
        for item in tag_rows:
            name = (item.get("name") or "").strip().lower()
            try:
                weight = int(item.get("count", 0) or 0)
            except (TypeError, ValueError):
                weight = 0
            if (not name or weight < 2 or name in
                    {"seen live", "favorites", "favourite", "favorite", "my favorites", "spotify", "lastfm"}):
                continue
            if name not in result:
                result.append(name)
            if len(result) >= 10:
                break
        return ", ".join(result), True
    except Exception:
        return "", False


def _pending_scout_tracks():
    with db_connect() as conn:
        return conn.execute("SELECT COUNT(*) FROM preferences WHERE item_type='track' AND COALESCE(scout_checked,0)=0").fetchone()[0]


def _enriched_genre(original, tags):
    if (original or '').strip().casefold() not in {'', 'unknown', 'music', 'rock', 'metal', 'pop', 'hip hop'} or not tags:
        return original
    return ', '.join(t.strip() for t in tags.split(',')[:5] if t.strip()) or original


def _scout_lastfm_info(method: str, **params):
    """Return (payload, completed). Never treat API/network failures as a check."""
    response = requests.get(
        "https://ws.audioscrobbler.com/2.0/",
        params={"method": method, "api_key": cfg.LASTFM_API_KEY.strip(),
                "autocorrect": "1", "format": "json", **params},
        timeout=10,
    )
    response.raise_for_status()
    payload = response.json()
    if "error" in payload:
        # Last.fm error 6 means this item is unknown, not a failed API request.
        if payload.get("error") == 6:
            return {}, True
        raise RuntimeError(f"Last.fm {method}: {payload.get('message', payload['error'])}")
    return payload, True


def _scout_names(entries, limit=8):
    if isinstance(entries, dict):
        entries = [entries]
    if not isinstance(entries, list):
        return ""
    names = []
    for entry in entries:
        name = entry.get('name', '').strip() if isinstance(entry, dict) else ''
        if name and name.casefold() not in {x.casefold() for x in names}:
            names.append(name)
        if len(names) >= limit:
            break
    return ', '.join(names)


def _scout_bio(text):
    clean = re.sub(r'<[^>]*>', ' ', text or '')
    return re.sub(r'\s+', ' ', clean).strip()[:300]


def _scout_merge_genre(current, track_tags, artist_tags):
    """Enrich a manually supplied genre with Scout tags, preferring track tags."""
    generic = {'', 'unknown', 'music'}
    # Do not mix artist-wide tags into a track that has its own tags.
    candidate = track_tags or artist_tags
    if not candidate:
        return current
    result = []
    seen = set()
    for source in (candidate, current or ''):
        for part in source.split(','):
            genre = part.strip()
            key = genre.casefold()
            if not genre or key in generic or key in seen:
                continue
            result.append(genre)
            seen.add(key)
            if len(result) >= 5:
                return ', '.join(result)
    return ', '.join(result) or current


def run_song_tag_enrichment():
    """Complete track and artist metadata before marking a track scout_checked."""
    time.sleep(2)
    while cfg.RUNNING:
        try:
            if not get_playback_policy().enrichment:
                time.sleep(5)
                continue
            if not getattr(cfg, 'SCOUT_ENABLED', True):
                time.sleep(5)
                continue
            if not cfg.LASTFM_API_KEY.strip():
                scout_display('🔭 Last.fm Scout ready', '⚠️ API-Key Error')
                time.sleep(5)
                continue
            db_path = _db_absolute_path()
            with db_connect() as conn:
                row = conn.execute(
                    "SELECT item_name FROM preferences WHERE item_type='track' "
                    "AND COALESCE(scout_checked,0)=0 ORDER BY score DESC, item_name LIMIT 1"
                ).fetchone()
            if not row:
                time.sleep(10)
                continue
            name = row[0]
            if ' - ' not in name:
                log_to_gui(f'⚠️ Scout: invalid track name: {name}')
                time.sleep(15)
                continue
            artist, title = name.split(' - ', 1)
            scout_display(f'🔭 Scout analyzing: {name}', 'Checking track and artist metadata')
            # Each request must succeed (or explicitly report item not found).
            # Otherwise leave scout_checked=0 so the entry can be retried.
            track_payload, _ = _scout_lastfm_info('track.getInfo', artist=artist, track=title)
            artist_payload, _ = _scout_lastfm_info('artist.getInfo', artist=artist)
            song_tags, tag_success = _fetch_track_top_tags(artist, title)
            if not tag_success:
                raise RuntimeError('Last.fm track.getTopTags did not complete')
            track = track_payload.get('track', {})
            artist_data = artist_payload.get('artist', {})
            track_info_tags = _scout_names(track.get('toptags', {}).get('tag', []))
            artist_tags = _scout_names(artist_data.get('tags', {}).get('tag', []))
            artist_similar = _scout_names(artist_data.get('similar', {}).get('artist', []))
            description = _scout_bio(track.get('wiki', {}).get('summary', '')) or _scout_bio(artist_data.get('bio', {}).get('summary', ''))
            raw_listeners = track.get('listeners') or artist_data.get('stats', {}).get('listeners', 0)
            try:
                listeners = max(0, int(raw_listeners))
            except (ValueError, TypeError):
                listeners = 0
            year = 0
            wiki_date = track.get('wiki', {}).get('published', '')
            match = re.search(r'\b(19\d{2}|20\d{2})\b', wiki_date)
            if match:
                year = int(match.group(1))
            if db_path != _db_absolute_path():
                continue
            with db_connect() as conn:
                row = conn.execute(
                    "SELECT genre,description,listeners,tags,similar,year,track_tags "
                    "FROM preferences WHERE item_name=? AND item_type='track'", (name,)
                ).fetchone()
                if row is None:
                    continue
                genre, old_desc, old_listeners, old_tags, old_similar, old_year, old_track_tags = row
                genre = _scout_merge_genre(genre, song_tags or track_info_tags, artist_tags)
                new_desc = old_desc or description
                new_listeners = old_listeners or listeners
                new_tags = old_tags or track_info_tags or artist_tags
                new_similar = old_similar or artist_similar
                new_year = old_year or year
                new_track_tags = song_tags or old_track_tags or track_info_tags
                conn.execute(
                    "UPDATE preferences SET genre=?, description=?, listeners=?, tags=?, "
                    "similar=?, year=?, track_tags=?, track_tag_checked_at=?, scout_checked=1 "
                    "WHERE item_name=? AND item_type='track' AND COALESCE(scout_checked,0)=0",
                    (genre, new_desc, new_listeners, new_tags, new_similar,
                     new_year, new_track_tags, time.time(), name),
                )
                # Add missing artist metadata without changing artist scores.
                artist_row = conn.execute(
                    "SELECT genre,description,listeners,tags,similar FROM preferences "
                    "WHERE item_name=? AND item_type='artist'", (artist,)
                ).fetchone()
                if artist_row:
                    ag, ad, al, at, asi = artist_row
                    conn.execute(
                        "UPDATE preferences SET genre=?,description=?,listeners=?,tags=?,similar=? "
                        "WHERE item_name=? AND item_type='artist'",
                        (_scout_merge_genre(ag, '', artist_tags), ad or _scout_bio(artist_data.get('bio', {}).get('summary', '')),
                         al or int(artist_data.get('stats', {}).get('listeners') or 0),
                         at or artist_tags, asi or artist_similar, artist),
                    )
                info = conn.execute(
                    "SELECT item_type,score,last_played,genre,video_id,duration,year,"
                    "description,listeners,tags,similar,track_tags FROM preferences "
                    "WHERE item_name=? AND item_type='track'", (name,)
                ).fetchone()
                artist_info = conn.execute(
                    "SELECT item_type,score,last_played,genre,video_id,duration,year,"
                    "description,listeners,tags,similar,track_tags FROM preferences "
                    "WHERE item_name=? AND item_type='artist'", (artist,)
                ).fetchone()
            if info:
                sync_track_to_chroma(name, *info)
            if artist_info:
                sync_track_to_chroma(artist, *artist_info)
            # ai_log(f'🎯 Scout fully checked: {name} | {genre} | {new_track_tags or "no track tags available"}')
            time.sleep(2)
        except Exception as exc:
            log_to_gui(f'⚠️ Scout track analysis (remains unchecked): {exc}')
            time.sleep(15)


def record_vibe_score(track: str, vibe: str, points: float):
    key = _vibe_key(vibe)
    if not key or not track or not points:
        return False
    try:
        with db_connect() as conn:
            conn.execute(
                "INSERT INTO vibe_feedback(track_key,vibe_key,vibe_score,interactions,last_updated) "
                "VALUES (?,?,?,1,?) "
                "ON CONFLICT(track_key,vibe_key) DO UPDATE SET "
                "vibe_score=MAX(0, MIN(100, vibe_score + ?)), "
                "interactions=interactions+1, last_updated=excluded.last_updated",
                (track, key, max(0.0, min(100.0, 50.0 + float(points))), time.time(), float(points)),
            )
        if gui.root is not None and gui.vibe_tree is not None:
            gui.root.after(0, refresh_vibe_scores_ui)
        return True
    except Exception as exc:
        log_to_gui(f"⚠️ Vibe score could not be recorded: {exc}")
        return False


def get_vibe_score(track: str, vibe: str) -> tuple[float, int]:
    key = _vibe_key(vibe)
    if not key or not track:
        return 50.0, 0
    try:
        with db_connect() as conn:
            row = conn.execute(
                "SELECT vibe_score, interactions FROM vibe_feedback WHERE track_key=? AND vibe_key=?",
                (track, key),
            ).fetchone()
        return (float(row[0]), int(row[1])) if row else (50.0, 0)
    except Exception:
        return 50.0, 0


# =============================================================================
# JUDGE EVALUATION & ROTATION BALANCING (INSTRUCTIONS.TXT)
# =============================================================================
def _evaluate_candidate_by_judge(
    meta: dict, target_vibe: str, raw_vibe: str, chroma_dist: float = None,
    genre_dist=None, vibe_learning=(50.0, 0)
) -> tuple[float, str]:
    vibe_sim = _cosine_similarity(chroma_dist)
    genre_sim = _cosine_similarity(genre_dist)
    personal = _normalized_affinity(meta.get('score', 10))
    target = (target_vibe or raw_vibe or '').strip()
    if target:
        alignment, info = _tag_concept_alignment(meta, f"{raw_vibe} {target_vibe}")
        vibe_fit = 0.76 * vibe_sim + 0.24 * alignment
        track_alignment, track_info = _spydertrack_judge_alignment(meta, f"{raw_vibe} {target_vibe}")
        profile_alignment, profile_info = _spydertrack_profile_alignment(meta, f"{raw_vibe} {target_vibe}")
        # Share the existing 12% SpyderTRACK budget; do not stack new bonuses.
        if profile_alignment is not None and track_alignment is not None:
            vibe_fit = 0.88 * vibe_fit + 0.08 * profile_alignment + 0.04 * track_alignment
        elif profile_alignment is not None:
            vibe_fit = 0.88 * vibe_fit + 0.12 * profile_alignment
        elif track_alignment is not None:
            vibe_fit = 0.88 * vibe_fit + 0.12 * track_alignment
        result = 100 * (0.60 * vibe_fit + 0.25 * genre_sim + 0.15 * personal)
        learned_score, interactions = vibe_learning
        context = _vibe_score_preference(learned_score, interactions)
        result += 24.0 * (context - 0.5)
        detail = (f"Semantic Vibe {vibe_fit:.0%} | Genre {genre_sim:.0%} | "
                  f"Global Taste {personal:.0%} ({float(meta.get('score', 10)):.0f} pts)\n"
                  f"Vibe Experience {learned_score:.0f}/100 ({interactions}x)\n"
                  f"{info}" + (f"\n{track_info}" if track_info else "")
                  + (f"\n{profile_info}" if profile_info else ""))
    else:
        result = 100 * (0.55 * genre_sim + 0.45 * personal)
        detail = (f"Genre {genre_sim:.0%} | Global Taste {personal:.0%} "
                  f"({float(meta.get('score', 10)):.0f} pts)")
    return result, detail


def _rotation_lottery(picked, scored_list, sqlite_candidates, current_vibe, raw_vibe,
                      t_cutoff, recent, banned_tracks, vibe_learning):
    """
    Exakte Umsetzung von ANLEITUNG.txt:
    1. Identify the artist and load ALL their tracks from SQLite.
    2. Determine C_max and C_min; check the 10% deviation.
    3. Below 10%: no rotation; keep the original decision.
    4. At 10% or above: check cooldowns/bans and apply the vibe filter.
    5. Gewichtete Lostrommel: L_i = C_max - C_i + 1.
    6. Shuffle and draw exactly once, then document the decision in the AI log.
    """
    name = picked[0]
    if " - " not in name:
        return picked
    artist = name.split(" - ", 1)[0].strip()
    artist_key = artist.casefold()

    # 1. Collect all tracks by the artist from the SQLite library
    band_rows = [
        row for key, row in sqlite_candidates.items()
        if " - " in row[0] and row[0].split(" - ", 1)[0].strip().casefold() == artist_key
    ]
    if len(band_rows) < 2:
        return picked

    # 2. Determine the artist's SQLite counts (Sections 2 and 8)
    with db_connect() as conn:
        counts = {
            k.casefold(): int(n)
            for k, n in conn.execute("SELECT track_key, judge_plays FROM judge_rotation")
        }

    play_counts = [counts.get(row[0].strip().casefold(), 0) for row in band_rows]
    c_max = max(play_counts)
    c_min = min(play_counts)

    if c_max == 0:
        return picked

    discrepancy = ((c_max - c_min) / c_max) * 100.0

    # 3. The 10% threshold (Section 2): no intervention below 10%
    if discrepancy < 10.0:
        return picked

    # 4. Rotationsausgleich ab 10 %: Kandidaten filtern (Section 4)
    scored_map = {item[0].casefold(): item for item in scored_list}
    suitable_candidates = []

    wanted_concepts = _concept_hits(current_vibe) if current_vibe else set()

    for row in band_rows:
        t_name = row[0].strip()
        t_key = t_name.casefold()

        # Cooldown & Blacklist respektieren (Section 4)
        if float(row[2] or 0) > t_cutoff or t_key in recent:
            continue
        if t_key in banned_tracks:
            continue

        # Score & Eignung ermitteln
        if t_key in scored_map:
            cand_score, cand_detail = scored_map[t_key][1], scored_map[t_key][2]
        else:
            meta = {
                "score": int(row[1] or 0),
                "last_played": float(row[2] or 0),
                "genre": (row[3] or "Music").strip(),
                "video_id": row[4] or "",
                "duration": int(row[5] or 0),
                "year": int(row[6] or 0),
                "description": (row[7] or "").strip(),
                "listeners": int(row[8] or 0),
                "tags": (row[9] or "").strip(),
                "track_tags": (row[10] or "").strip(),
            }
            cand_score, cand_detail = _evaluate_candidate_by_judge(
                meta, current_vibe, raw_vibe,
                chroma_dist=None, genre_dist=None,
                vibe_learning=vibe_learning.get(t_key, (50.0, 0))
            )

        # Vibe-Filter (Section 4): Konzept-Konflikte aussortieren
        if wanted_concepts:
            all_tags = (row[10] or "") or (row[9] or "")
            observed_concepts = _concept_hits(all_tags)
            conflicts = sum(1 for a, b in VIBE_OPPOSITES if (a in wanted_concepts and b in observed_concepts) or (b in wanted_concepts and a in observed_concepts))
            if conflicts > 0:
                continue

        # Mindesteignung
        if cand_score < max(25.0, picked[1] - 25.0):
            continue

        suitable_candidates.append((t_name, cand_score, cand_detail))

    if len(suitable_candidates) < 2:
        return picked

    # 5. Gewichtete Candidate pool (Section 5 & 6)
    weights = [
        max(1, c_max - counts.get(c[0].casefold(), 0) + 1)
        for c in suitable_candidates
    ]

    # 6. Shuffle exactly once and draw randomly (Section 7)
    tickets = list(zip(suitable_candidates, weights))
    random.shuffle(tickets)
    drawn = random.choices(
        [item for item, _ in tickets],
        weights=[w for _, w in tickets],
        k=1,
    )[0]

    cand_log = ", ".join(
        f"{c[0].split(' - ', 1)[1] if ' - ' in c[0] else c[0]} ({counts.get(c[0].casefold(), 0)}p | {w}L)"
        for c, w in tickets
    )
    ai_log(
        f"🎰 JUDGE ROTATION ({artist} | D={discrepancy:.1f}%):\n"
        f"   Candidate pool ({len(tickets)} tracks): {cand_log}\n"
        f"   Selected: {drawn[0]}"
    )
    return drawn


# =============================================================================
# HISTORICAL NOTE:
# This entire thing started with one innocent question:
#
#     "Can AI recommend music to me too? 🤔"
#
# Things escalated.
# =============================================================================

# =============================================================================
# AUTONOMOUS CHROMADB BRAIN & DYNAMIC PYTHON JUDGE
# =============================================================================

def ask_gemini_for_next_track():
    return ask_chroma_for_next_track()


def ask_chroma_for_next_track() -> str | None:
    collection = chroma_collection
    if collection is None:
        return get_fallback_track()

    now = time.time()
    t_cutoff = now - cfg.TRACK_COOLDOWN_MINUTES * 60
    a_cutoff = now - cfg.ARTIST_COOLDOWN_MINUTES * 60
    active_genres = cfg.TARGET_GENRES
    if getattr(cfg, 'SELECTED_GENRE_PRESET', 'Custom') != 'Custom':
        presets = {}
        for item in getattr(cfg, 'GENRE_PRESETS', '').split(' | '):
            if ':' in item:
                k, v = item.split(':', 1)
                presets[k.strip()] = v.strip()
        active_genres = presets.get(cfg.SELECTED_GENRE_PRESET, active_genres)

    raw_vibe = cfg.SELECTED_PROMPT_PREF.strip()
    current_vibe = resolve_vibe_query(raw_vibe) if raw_vibe else ''
    key = _vibe_key(raw_vibe)
    with db_connect() as conn:
        blocked_rows = conn.execute(
            "SELECT item_name, last_played FROM preferences "
            "WHERE item_type='artist' AND last_played > ? ORDER BY last_played DESC",
            (a_cutoff,)
        ).fetchall()
        recent_rows = conn.execute(
            "SELECT item_name, last_played FROM preferences "
            "WHERE item_type='track' AND last_played > ? ORDER BY last_played DESC",
            (t_cutoff,)
        ).fetchall()
        blocked = {name.casefold() for name, _ in blocked_rows}
        recent = {name.casefold() for name, _ in recent_rows}
        blacklist_rows = conn.execute("SELECT item_name, item_type FROM blacklist").fetchall()
        banned_tracks = {name.strip().casefold() for name, kind in blacklist_rows if kind == "track" and name}
        banned_artists = {name.strip().casefold() for name, kind in blacklist_rows if kind == "artist" and name}
        vibe_learning = {}
        if key:
            vibe_learning = {r[0].casefold(): (float(r[1]), int(r[2])) for r in conn.execute(
                "SELECT track_key,vibe_score,interactions FROM vibe_feedback WHERE vibe_key=?", (key,)
            )}

    context_description = f"{raw_vibe} -> [{current_vibe}]" if current_vibe else "General Mixed Vibe"
    ai_log("\n" + "═" * 45 + "\n"
           "📤 CHROMADB SELECTION\n"
           f"   Genre: {active_genres}\n"
           f"   Vibe:  {context_description}\n"
           f"   Temperature: {getattr(cfg, 'TEMPERATURE', 0.3):.1f}\n"
           + "─" * 45)

    def lock_details(rows, minutes):
        if not rows:
            return "   None"
        return "\n".join(
            f"   • {name} ({max(1, math.ceil((played + minutes * 60 - now) / 60))}m left)"
            for name, played in rows
        )

    ai_log(f"🔒 LOCKED SONGS ({len(recent_rows)} | {cfg.TRACK_COOLDOWN_MINUTES} min):\n"
           f"{lock_details(recent_rows, cfg.TRACK_COOLDOWN_MINUTES)}\n")
    ai_log(f"🔒 LOCKED ARTISTS ({len(blocked_rows)} | {cfg.ARTIST_COOLDOWN_MINUTES} min):\n"
           f"{lock_details(blocked_rows, cfg.ARTIST_COOLDOWN_MINUTES)}\n"
           + "─" * 45)
    try:
        if collection.count() == 0:
            return get_fallback_track()
        queries = [f"Genre and music style: {active_genres}."]
        if current_vibe:
            queries.insert(0, f"Song mood and musical attitude: {current_vibe}.")
        results = collection.query(
            query_texts=queries, n_results=90,
            where={"item_type": "track"},
            include=["metadatas", "distances"],
        )
        hits = {}
        for qi, ids in enumerate(results.get('ids', [])):
            for name, meta, dist in zip(ids,
                                        results['metadatas'][qi],
                                        results['distances'][qi]):
                if not name:
                    continue
                hit_key = name.strip().casefold()
                entry = hits.setdefault(
                    hit_key,
                    {'name': name.strip(), 'meta': meta or {}, 'vibe': None, 'genre': None}
                )
                dimension = 'vibe' if current_vibe and qi == 0 else 'genre'
                previous = entry[dimension]
                if previous is None or dist < previous:
                    entry[dimension] = dist

        with db_connect() as conn:
            sqlite_candidates = {}
            for row in conn.execute(
                "SELECT item_name, score, last_played, genre, video_id, duration, year, "
                "description, listeners, tags, track_tags, spydertrack_data, spydertrack_sync_status FROM preferences "
                "WHERE item_type='track'"
            ):
                sqlite_candidates[row[0].strip().casefold()] = row

        scored = []
        for hit_key, data in hits.items():
            name = data['name']
            meta = data['meta']
            sqlite_row = sqlite_candidates.get(hit_key)
            if sqlite_row:
                name = sqlite_row[0].strip()
                meta = dict(meta)
                meta.update({
                    "score": int(sqlite_row[1] or 0),
                    "last_played": float(sqlite_row[2] or 0),
                    "genre": (sqlite_row[3] or "Music").strip(),
                    "video_id": sqlite_row[4] or "",
                    "duration": int(sqlite_row[5] or 0),
                    "year": int(sqlite_row[6] or 0),
                    "description": (sqlite_row[7] or "").strip(),
                    "listeners": int(sqlite_row[8] or 0),
                    "tags": (sqlite_row[9] or "").strip(),
                    "track_tags": (sqlite_row[10] or "").strip(),
                })
                if sqlite_row[12] in (1, 2) and sqlite_row[11]:
                    try:
                        research = json.loads(sqlite_row[11])
                        evidence_text, feature_count, source_count = _spydertrack_judge_evidence(research)
                        meta['_spydertrack_evidence'] = evidence_text
                        meta['_spydertrack_feature_count'] = feature_count
                        meta['_spydertrack_source_count'] = source_count
                        if isinstance(research, dict):
                            meta['_spydertrack_vibe_profile'] = research.get('vibe_profile') or {}
                    except (ValueError, TypeError):
                        pass
            if float(meta.get('last_played', 0)) > t_cutoff or name.casefold() in recent:
                continue
            artist = name.split(' - ', 1)[0].strip().casefold()
            if name.casefold() in banned_tracks or artist in banned_artists:
                continue
            if artist in blocked:
                continue
            rank, detail = _evaluate_candidate_by_judge(
                meta, current_vibe, raw_vibe,
                data['vibe'], data['genre'], vibe_learning.get(name.casefold(), (50.0, 0))
            )
            scored.append((name, rank, detail))

        if scored:
            scored.sort(key=lambda item: item[1], reverse=True)
            ai_log("🔎 JUDGE TOP CANDIDATES")
            for pos, (candidate_name, candidate_score, candidate_detail) in enumerate(scored[:8], 1):
                detail_lines = candidate_detail.replace("\n", "\n       ")
                ai_log(f"{pos}. {candidate_name} -> {candidate_score:.1f}/100\n"
                       f"       {detail_lines}\n")
            ai_log("─" * 45)
            temperature = _clamp(getattr(cfg, 'TEMPERATURE', 0.3), 0.1, 1.0)
            pool_size = max(1, min(len(scored), max(12, int(len(scored) * (0.15 + 0.65 * temperature)))))
            pool = scored[:pool_size]
            softness = 2.0 + temperature * 15.0
            best = pool[0][1]
            weights = [math.exp((item[1] - best) / softness) for item in pool]
            picked = random.choices(pool, weights=weights, k=1)[0]
            
            # Rotationsausgleich anwenden (Section 3 & 11)
            try:
                picked = _rotation_lottery(
                    picked, scored, sqlite_candidates,
                    current_vibe, raw_vibe, t_cutoff, recent, banned_tracks, vibe_learning
                )
            except Exception as exc:
                ai_log(f"⚠️ Rotation skipped; Judge selection retained: {exc}")
                
            selection_detail = picked[2]
            ai_log(f"⚖️ JUDGE SELECTION\n\n"
                   f"Track: {picked[0]}\n"
                   f"Score: {picked[1]:.1f}/100\n"
                   f"{selection_detail}\n"
                   f"Candidate pool: {len(pool)}/{len(scored)}\n"
                   + "═" * 45 + "\n")
            return picked[0]
    except Exception as exc:
        ai_log(f"⚠️ Chroma query error: {exc}")
    fallback = get_fallback_track()
    ai_log(f"🛟 Fallback: {fallback}")
    return fallback


# =============================================================================
# LAST.FM AUTONOMOUS SCOUT
# =============================================================================
def run_lastfm_scout():
    """Discover through favorite artists, then expand the stored similar-artist graph."""
    validated_scout_key = None
    # Failed candidates cool down; they are not permanently blacklisted.
    candidate_retry_at = {}
    favorite_retry_at = {}
    artist_discovery_cooldown_seconds = 20 * 60
    scout_display("🔭 Last.fm Scout ready", "Initializing...")
    time.sleep(8)

    def norm(value):
        return (value or "").strip().casefold()

    def api(method, timeout=10, **params):
        response = requests.get(
            'https://ws.audioscrobbler.com/2.0/',
            params=dict(method=method, api_key=cfg.LASTFM_API_KEY.strip(), format='json', **params),
            timeout=timeout,
        )
        response.raise_for_status()
        data = response.json()
        if data.get('error'):
            raise ValueError(f"Last.fm {method}: {data.get('message', data['error'])}")
        return data

    while cfg.RUNNING:
        sleep_interval = 30
        try:
            if not get_playback_policy().discovery:
                scout_display('🔭 Last.fm Scout', 'Discovery disabled in local playback mode')
                time.sleep(5)
                continue
            if not getattr(cfg, 'SCOUT_ENABLED', True):
                scout_display('🔭 Last.fm Scout ready', 'Scout disabled')
                time.sleep(5)
                continue
            if not cfg.LASTFM_API_KEY.strip():
                scout_display('🔭 Last.fm Scout ready', '⚠️ API-Key Error')
                time.sleep(5)
                continue
            key = cfg.LASTFM_API_KEY.strip()
            if key != validated_scout_key:
                try:
                    api('artist.getInfo', timeout=8, artist='Eclipse')
                    validated_scout_key = key
                except Exception as exc:
                    scout_display('🔭 Last.fm Scout ready', f'⚠️ Last.fm validation: {exc}')
                    time.sleep(10)
                    continue

            pending = _pending_scout_tracks()
            if pending:
                scout_display('🔭 Scout track analysis', f'{pending} tracks unchecked | Discovery waiting')
                time.sleep(5)
                continue

            active_targets = [
                g.strip().casefold() for g in getattr(cfg, 'TARGET_GENRES', '').split(',') if g.strip()
            ]
            top_limit = max(1, int(getattr(cfg, 'TOP_FAV_ARTISTS_COUNT', 100)))
            with db_connect() as conn:
                # Migration is idempotent and safe for existing music.db files.
                conn.execute(
                    "CREATE TABLE IF NOT EXISTS scout_artist_discovery_cooldown "
                    "(artist_key TEXT PRIMARY KEY, retry_at REAL NOT NULL)"
                )
                artist_rows = conn.execute(
                    "SELECT item_name, score FROM preferences WHERE item_type = 'artist' ORDER BY score DESC"
                ).fetchall()
                known_tracks = {
                    norm(row[0]) for row in conn.execute(
                        "SELECT item_name FROM preferences WHERE item_type = 'track'"
                    ).fetchall()
                }
                similar_rows = conn.execute(
                    "SELECT similar FROM preferences WHERE similar IS NOT NULL AND similar != ''"
                ).fetchall()
                cooldown_rows = conn.execute(
                    "SELECT artist_key FROM scout_artist_discovery_cooldown WHERE retry_at > ?",
                    (time.time(),)
                ).fetchall()

            cooling_artists = {row[0] for row in cooldown_rows}

            known_artists = {norm(row[0]) for row in artist_rows}
            favorites = [row[0] for row in artist_rows if row[1] > 10][:top_limit]
            if not favorites:
                favorites = [row[0] for row in artist_rows[:top_limit]]

            # Persistent source of leads: each discovered artist can add new edges.
            graph_pool = {}
            for (similar_text,) in similar_rows:
                for name in similar_text.split(','):
                    name = name.strip()
                    if name and norm(name) not in cooling_artists:
                        graph_pool.setdefault(norm(name), name)

            now = time.time()
            candidate_retry_at = {k: v for k, v in candidate_retry_at.items() if v > now}
            favorite_retry_at = {k: v for k, v in favorite_retry_at.items() if v > now}
            pool = []
            source_info = ''

            # Stage 1: try a fresh favorite, not the same exhausted one every cycle.
            fresh_favorites = [a for a in favorites if norm(a) not in favorite_retry_at]
            if fresh_favorites:
                target_artist = random.choice(fresh_favorites)
                favorite_retry_at[norm(target_artist)] = now + 15 * 60
                try:
                    result = api('artist.getsimilar', artist=target_artist, limit=50)
                    similar = result.get('similarartists', {}).get('artist', [])
                    if isinstance(similar, dict):
                        similar = [similar]
                    for item in similar:
                        name = item.get('name', '').strip()
                        try:
                            match = float(item.get('match', 0))
                        except (ValueError, TypeError):
                            match = 0
                        if (name and match >= 0.35 and norm(name) not in cooling_artists
                                and norm(name) not in candidate_retry_at):
                            pool.append(name)
                    if pool:
                        source_info = f'Similar to {target_artist}'
                except Exception as exc:
                    scout_display('🔭 Last.fm Scout', f'Favorite lookup failed: {exc}')

            # Stage 2: previously discovered graph edges, not only the Top X loop.
            if not pool:
                pool = [name for key, name in graph_pool.items()
                        if key not in candidate_retry_at and key not in cooling_artists]
                if pool:
                    source_info = 'SQLite Network Discovery'
                    scout_display('🔭 Deep Scout', f'{len(pool)} eligible similar artists available')

            if not pool:
                scout_display('🔭 Last.fm Scout ready', 'No new candidates available; retrying later')
                time.sleep(30)
                continue

            # Deduplicate Live Similar results and enforce cooldown for both sources.
            pool = list({norm(name): name for name in pool
                         if norm(name) not in cooling_artists}.values())
            random.shuffle(pool)
            discovered = None
            for candidate_artist in pool[:12]:
                key_name = norm(candidate_artist)
                # Short retry for transient API failures; longer for exhausted candidates.
                try:
                    a_data = api('artist.getInfo', timeout=8, artist=candidate_artist).get('artist', {})
                    raw_tags = a_data.get('tags', {}).get('tag', [])
                    if isinstance(raw_tags, dict):
                        raw_tags = [raw_tags]
                    tags_list = [t.get('name', '').strip().lower() for t in raw_tags[:5] if t.get('name')]
                    if active_targets and tags_list and not any(
                        target in tag or tag in target for tag in tags_list for target in active_targets
                    ):
                        candidate_retry_at[key_name] = time.time() + 6 * 3600
                        continue

                    candidate_genre = (tags_list[0].title() if tags_list else
                                       active_targets[0].title() if active_targets else 'Music')
                    bio_text = a_data.get('bio', {}).get('summary', '')
                    bio_clean = re.sub(r'<a\b[^>]*>.*?</a>', '', bio_text, flags=re.DOTALL | re.IGNORECASE)
                    bio_clean = re.sub(r'<[^>]+>', '', bio_clean)
                    bio_clean = re.sub(r'\s+', ' ', bio_clean).strip()
                    description = bio_clean[:297] + '...' if len(bio_clean) > 300 else bio_clean
                    sim_data = a_data.get('similar', {}).get('artist', [])
                    if isinstance(sim_data, dict):
                        sim_data = [sim_data]
                    discovered_similar = [v.get('name', '').strip() for v in sim_data[:10] if v.get('name')]
                    listener_value = a_data.get('stats', {}).get('listeners', '0')
                    listeners = int(listener_value) if str(listener_value).isdigit() else 0

                    t_data = api('artist.gettoptracks', artist=candidate_artist, limit=20)
                    tracks = t_data.get('toptracks', {}).get('track', [])
                    if isinstance(tracks, dict):
                        tracks = [tracks]
                    unheard = []
                    for tr in tracks:
                        raw_title = tr.get('name', '').strip()
                        title = re.sub(
                            r'[\(\[\{].*?(remaster|bonus|live|edit|deluxe).*?[\)\]\}]',
                            '', raw_title, flags=re.IGNORECASE
                        ).strip()
                        if title and norm(f'{candidate_artist} - {title}') not in known_tracks:
                            unheard.append(title)
                    if not unheard:
                        candidate_retry_at[key_name] = time.time() + 6 * 3600
                        continue

                    track = random.choice(unheard[:10])
                    discovered = (candidate_artist, track, candidate_genre, description,
                                  listeners, tags_list, discovered_similar)
                    # The persistent cooldown starts only after the track is saved.
                    break
                except Exception as exc:
                    candidate_retry_at[key_name] = time.time() + 5 * 60
                    scout_display('🔭 Last.fm Scout', f'{candidate_artist}: {exc}')
                    continue

            if not discovered:
                sleep_interval = 10
                continue

            (discovered_artist, discovered_track, discovered_genre, discovered_description,
             discovered_listeners, discovered_tags, discovered_similar) = discovered
            discovered_year = 0
            # Keep the original track-wiki and album-wiki year fallbacks.
            try:
                clean_t = re.sub(r'[\(\[\{].*?[\)\]\}]', '', discovered_track).strip()
                info_url = (
                    f"https://ws.audioscrobbler.com/2.0/?method=track.getInfo"
                    f"&artist={requests.utils.quote(discovered_artist)}"
                    f"&track={requests.utils.quote(clean_t)}"
                    f"&api_key={cfg.LASTFM_API_KEY}&format=json"
                )
                info_res = requests.get(info_url, timeout=5)
                if info_res.status_code == 200:
                    tr_data = info_res.json().get("track", {})
                    wiki_date = tr_data.get("wiki", {}).get("published", "")
                    year_match = re.search(r'\b(19\d{2}|20\d{2})\b', wiki_date)
                    if year_match:
                        discovered_year = int(year_match.group(1))
                    elif "album" in tr_data:
                        alb_title = tr_data["album"].get("title", "")
                        if alb_title:
                            alb_url = (
                                f"https://ws.audioscrobbler.com/2.0/?method=album.getInfo"
                                f"&artist={requests.utils.quote(discovered_artist)}"
                                f"&album={requests.utils.quote(alb_title)}"
                                f"&api_key={cfg.LASTFM_API_KEY}&format=json"
                            )
                            alb_res = requests.get(alb_url, timeout=5)
                            if alb_res.status_code == 200:
                                alb_info = alb_res.json().get("album", {})
                                m_alb = re.search(r'\b(19\d{2}|20\d{2})\b', alb_info.get("wiki", {}).get("published", ""))
                                if m_alb:
                                    discovered_year = int(m_alb.group(1))
                                else:
                                    m_all = re.findall(r'\b(19\d{2}|20\d{2})\b', str(alb_info))
                                    v_years = [int(y) for y in m_all if 1950 <= int(y) <= 2026]
                                    if v_years:
                                        discovered_year = min(v_years)
            except Exception:
                discovered_year = 0

            track_name = f'{discovered_artist.strip()} - {discovered_track.strip()}'
            tags_str = ', '.join(discovered_tags)
            sim_str = ', '.join(discovered_similar)
            song_tags, tags_checked = _fetch_track_top_tags(discovered_artist, discovered_track)

            with db_connect() as conn:
                artist_exists = conn.execute(
                    "SELECT 1 FROM preferences WHERE item_type = 'artist' AND item_name = ?",
                    (discovered_artist.strip(),)
                ).fetchone()
                track_exists = conn.execute(
                    "SELECT 1 FROM preferences WHERE item_type = 'track' AND item_name = ?",
                    (track_name,)
                ).fetchone()
                if track_exists:
                    sleep_interval = 10
                    continue
                if not artist_exists:
                    conn.execute(
                        "INSERT OR IGNORE INTO preferences (item_name, item_type, score, last_played, genre, year, description, listeners, tags, similar) "
                        "VALUES (?, 'artist', 10, 0, ?, 0, ?, ?, ?, ?)",
                        (discovered_artist.strip(), discovered_genre, discovered_description,
                         discovered_listeners, tags_str, sim_str)
                    )
                cursor = conn.execute(
                    "INSERT OR IGNORE INTO preferences (item_name, item_type, score, last_played, genre, year, description, listeners, tags, similar, track_tags, track_tag_checked_at) "
                    "VALUES (?, 'track', 10, 0, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (track_name, discovered_genre, discovered_year, discovered_description,
                     discovered_listeners, tags_str, sim_str, song_tags,
                     time.time() if tags_checked else 0)
                )
                if cursor.rowcount != 1:
                    sleep_interval = 10
                    continue
                conn.execute(
                    "INSERT INTO scout_artist_discovery_cooldown (artist_key, retry_at) "
                    "VALUES (?, ?) ON CONFLICT(artist_key) DO UPDATE SET retry_at=excluded.retry_at",
                    (norm(discovered_artist), time.time() + artist_discovery_cooldown_seconds)
                )

            if not artist_exists:
                sync_track_to_chroma(
                    discovered_artist.strip(), 'artist', 10, 0.0, discovered_genre,
                    year=0, description=discovered_description, listeners=discovered_listeners,
                    tags=tags_str, similar=sim_str
                )
            sync_track_to_chroma(
                track_name, 'track', 10, 0.0, discovered_genre,
                year=discovered_year, description=discovered_description,
                listeners=discovered_listeners, tags=tags_str, similar=sim_str,
                track_tags=song_tags
            )
            year_info = f' | {discovered_year}' if discovered_year else ''
            scout_display(f'🔭 {track_name}',
                          f'[{source_info}] +10 pts | {discovered_genre}{year_info} | ChromaDB synced')

        except Exception as err:
            scout_display('⚠️ Scout error', str(err))
            sleep_interval = 10

        time.sleep(sleep_interval)


# =============================================================================
# FALLBACK TRACK SELECTION
# =============================================================================
def get_fallback_track() -> str:
    log_to_gui("🛟 Safety: Initiating fallback track…")
    try:
        t_cutoff = time.time() - cfg.TRACK_COOLDOWN_MINUTES * 60
        a_cutoff = time.time() - cfg.ARTIST_COOLDOWN_MINUTES * 60

        with db_connect() as conn:
            blocked_artists = [
                row[0].lower() for row in conn.execute(
                    "SELECT item_name FROM preferences "
                    "WHERE item_type = 'artist' AND last_played > ?",
                    (a_cutoff,),
                ).fetchall()
            ]
            candidates = [
                row[0] for row in conn.execute(
                    "SELECT item_name FROM preferences "
                    "WHERE item_type = 'track' AND last_played <= ? "
                    "ORDER BY score DESC LIMIT 30",
                    (t_cutoff,),
                ).fetchall()
            ]
            blacklist_rows = conn.execute("SELECT item_name, item_type FROM blacklist").fetchall()
            banned_tracks = {name.strip().casefold() for name, kind in blacklist_rows if kind == "track" and name}
            banned_artists = {name.strip().casefold() for name, kind in blacklist_rows if kind == "artist" and name}

        valid = []
        for t in candidates:
            t_artist = t.split(" - ", 1)[0].strip().lower()
            if (t.casefold() not in banned_tracks and t_artist not in banned_artists
                    and t_artist not in blocked_artists):
                valid.append(t)
                
        if valid:
            pick = random.choice(valid[:10])
            log_to_gui(f"🛟 Fallback: {pick}")
            return pick
    except Exception:
        pass

    return ""


def _parse_duration(duration_val) -> int:
    if not duration_val: return 0
    if isinstance(duration_val, int): return duration_val
    parts = str(duration_val).split(':')
    if len(parts) == 2:
        return int(parts[0]) * 60 + int(parts[1])
    elif len(parts) == 3:
        return int(parts[0]) * 3600 + int(parts[1]) * 60 + int(parts[2])
    return 0


# Wikidata artist alias resolution: structured identity, no biography parsing.
_WIKIDATA_API = "https://www.wikidata.org/w/api.php"
_WIKIDATA_BAND_TYPES = {"Q215380", "Q5741069", "Q641066", "Q216337", "Q2088357", "Q105756498", "Q2393314"}
_WIKIDATA_MUSIC_OCCUPATIONS = {"Q177220", "Q488205", "Q49757", "Q753110", "Q36834", "Q639669", "Q2252262", "Q855091", "Q66715801", "Q33999", "Q10798782"}
_wikidata_request_lock = threading.Lock()
_wikidata_last_request = 0.0


def _wikidata_request(params: dict) -> dict:
    """Throttled Wikidata Action API request; network errors propagate to caller."""
    import urllib.parse
    import urllib.request
    global _wikidata_last_request
    with _wikidata_request_lock:
        wait = 1.1 - (time.monotonic() - _wikidata_last_request)
        if wait > 0:
            time.sleep(wait)
        url = _WIKIDATA_API + "?" + urllib.parse.urlencode({**params, "format": "json"})
        contact = cfg.WIKIDATA_CONTACT_EMAIL.strip()
        if not re.fullmatch(r"[^\s@]+@[^\s@]+\.[^\s@]+", contact):
            raise ValueError("Wikidata contact email is missing or invalid. Set it in Expert Settings.")
        request = urllib.request.Request(
            url, headers={"User-Agent": f"SpyderMUSIC/4.0 (contact: {contact})",
                          "Accept": "application/json"})
        _wikidata_last_request = time.monotonic()
        with urllib.request.urlopen(request, timeout=12) as response:
            return json.load(response)


def _wikidata_normalize(value: str) -> str:
    return "".join(ch for ch in unicodedata.normalize("NFKC", value).casefold()
                   if ch.isalnum())


def _wikidata_values(entity: dict, field: str, language: str) -> list[str]:
    value = entity.get(field, {}).get(language)
    if field == "labels":
        return [value["value"]] if isinstance(value, dict) and value.get("value") else []
    return [item["value"] for item in (value or [])
            if isinstance(item, dict) and item.get("value")]


def _wikidata_claim_ids(entity: dict, prop: str) -> set[str]:
    result = set()
    for statement in entity.get("claims", {}).get(prop, []):
        if statement.get("rank") == "deprecated":
            continue
        value = statement.get("mainsnak", {}).get("datavalue", {}).get("value")
        if isinstance(value, dict) and value.get("id"):
            result.add(value["id"])
    return result


def _wikidata_latin_artist(artist: str) -> str | None:
    """Resolve non-Latin artist names via Wikidata's structured artist entities.

    Fail closed on ambiguity. Reuse the existing persistent artist_aliases table.
    """
    if not any(unicodedata.category(ch).startswith("L") and
               "LATIN" not in unicodedata.name(ch, "") for ch in artist):
        return None

    try:
        with db_connect() as conn:
            row = conn.execute(
                "SELECT resolved_name FROM artist_aliases WHERE original_name = ?", (artist,)
            ).fetchone()
        if row and row[0]:
            log_to_gui(f"💾 [Wikidata] Cached artist alias: '{artist}' → '{row[0]}'")
            return row[0]
    except sqlite3.Error as exc:
        log_to_gui(f"⚠️ [Wikidata] Alias cache read failed: {exc}")

    try:
        search = _wikidata_request({"action": "wbsearchentities", "search": artist,
                                    "language": "ko", "uselang": "ko", "type": "item", "limit": 10})
        ids = list(dict.fromkeys(item["id"] for item in search.get("search", [])
                                 if item.get("id", "").startswith("Q")))
        if not ids:
            log_to_gui(f"🌐 [Wikidata] No artist candidates for '{artist}'")
            return None
        details = _wikidata_request({"action": "wbgetentities", "ids": "|".join(ids),
                                     "props": "labels|aliases|claims", "languages": "ko|en"})
        eligible = []
        for qid in ids:
            entity = details.get("entities", {}).get(qid, {})
            if "missing" in entity:
                continue
            names_ko = (_wikidata_values(entity, "labels", "ko") +
                        _wikidata_values(entity, "aliases", "ko"))
            names_en = (_wikidata_values(entity, "labels", "en") +
                        _wikidata_values(entity, "aliases", "en"))
            if _wikidata_normalize(artist) not in {
                    _wikidata_normalize(name) for name in names_ko + names_en}:
                continue
            kinds = _wikidata_claim_ids(entity, "P31")
            jobs = _wikidata_claim_ids(entity, "P106")
            is_group = bool(kinds & _WIKIDATA_BAND_TYPES)
            is_solo = "Q5" in kinds and bool(jobs & _WIKIDATA_MUSIC_OCCUPATIONS)
            if not (is_group or is_solo):
                continue
            en_label = _wikidata_values(entity, "labels", "en")
            ko_latin = [name for name in names_ko
                        if any("LATIN" in unicodedata.name(ch, "") for ch in name)
                        and not any("HANGUL" in unicodedata.name(ch, "") for ch in name)]
            resolved = (en_label or ko_latin or [None])[0]
            if resolved and any("LATIN" in unicodedata.name(ch, "") for ch in resolved) and not any(
                    unicodedata.category(ch).startswith("L") and
                    "LATIN" not in unicodedata.name(ch, "") for ch in resolved):
                eligible.append((qid, resolved.strip()))

        if len(eligible) != 1:
            log_to_gui(f"🌐 [Wikidata] {'Ambiguous artist identity' if eligible else 'No safe artist match'} "
                       f"for '{artist}' ({len(eligible)} candidates)")
            return None
        qid, resolved = eligible[0]
        try:
            with db_connect() as conn:
                conn.execute(
                    "INSERT INTO artist_aliases (original_name, resolved_name, resolved_at) "
                    "VALUES (?, ?, ?) ON CONFLICT(original_name) DO UPDATE SET "
                    "resolved_name=excluded.resolved_name, resolved_at=excluded.resolved_at",
                    (artist, resolved, time.time()))
            log_to_gui(f"💾 [Wikidata] Artist alias saved: '{artist}' → '{resolved}' ({qid})")
        except sqlite3.Error as exc:
            log_to_gui(f"⚠️ [Wikidata] Alias cache write failed: {exc}")
        return resolved
    except Exception as exc:
        log_to_gui(f"⚠️ [Wikidata] Alias lookup failed: {exc}")
        return None


def search_ytm_track(query: str, genre_info: str = "Unknown", *,
                     _wikidata_retry: bool = False,
                     _original_query: str | None = None) -> tuple[str | None, int, str, str]:
    query_key = _original_query or query.strip()

    def try_artist_alias():
        if _wikidata_retry or " - " not in query:
            return None
        artist, title = query.split(" - ", 1)
        alternate_artist = _wikidata_latin_artist(artist.strip())
        if not alternate_artist:
            return None
        new_query = f"{alternate_artist} - {title.strip()}"
        log_to_gui(f"🌐 [Wikidata] Artist alias: '{artist}' → '{alternate_artist}'")
        return search_ytm_track(new_query, genre_info,
                                _wikidata_retry=True, _original_query=query_key)

    try:
        with db_connect() as conn:
            row = conn.execute(
                "SELECT video_id, duration, item_name, local FROM preferences "
                "WHERE item_name = ? AND item_type = 'track' AND duration > 0",
                (query_key,)
            ).fetchone()

            if row:
                vid_id, dur, db_name, is_local = row
                dur = int(dur)
                parts = db_name.split(" - ", 1)
                cached_artist = parts[0].strip()
                cached_title = parts[1].strip() if len(parts) > 1 else "?"

                safe_query_name = re.sub(r'[\\/*?:"<>|]', "", db_name)
                local_file = os.path.join(DATA_DIR, "cache", f"{safe_query_name}.webm")

                if os.path.exists(local_file) and os.path.getsize(local_file) > 100000:
                    if is_local != 1:
                        conn.execute(
                            "UPDATE preferences SET local = 1 WHERE item_name = ?",
                            (db_name,)
                        )
                    log_to_gui(f"🚀 [💥 LOCAL OFFLINE] Direct-Play: '{db_name}'")
                    return local_file, dur, cached_artist, cached_title

                if is_local == 1:
                    conn.execute(
                        "UPDATE preferences SET local = 0 WHERE item_name = ?",
                        (db_name,)
                    )
                    log_to_gui(
                        f"🛡️ [Shield] Local cache file missing. Resetting local=0 "
                        f"for '{db_name}'."
                    )

                if vid_id:
                    log_to_gui(
                        f"⚡ [DB Cache] Fast-Load Stream: '{db_name}' (ID: {vid_id})"
                    )
                    return vid_id, dur, cached_artist, cached_title
    except Exception as e:
        print(f"DB Read Error in Cache Search: {e}")

    log_to_gui(f"🔎 [YTMusicAPI] Searching: {query}")

    def clean_noise(text: str) -> str:
        text = re.sub(r'[\(\[\{][^\)\]\}]*(remaster|official|video|audio|edit|lyrics?)[^\)\]\}]*[\)\]\}]', '', text, flags=re.IGNORECASE)
        text = re.sub(r'\s-\s+(radio|live|edit|album|remastered|official|extended|mix|audio|video).*', '', text, flags=re.IGNORECASE)
        text = re.sub(r'\b(remaster|remastered|hd|4k|audio|video|official|lyrics)\b', '', text, flags=re.IGNORECASE)
        text = re.sub(r'\s+', ' ', text)
        if text.endswith(' -'):
            text = text[:-2]
        return text.strip()

    # Prefer a previously verified artist alias for similarity scoring.
    # Keep the original query intact for YTM search, cache lookup and DB migration.
    similarity_query = query
    if " - " in query:
        source_artist, source_title = query.split(" - ", 1)
        try:
            with db_connect() as conn:
                alias_row = conn.execute(
                    "SELECT resolved_name FROM artist_aliases WHERE original_name = ?",
                    (source_artist.strip(),)
                ).fetchone()
            if alias_row and alias_row[0]:
                similarity_query = f"{alias_row[0]} - {source_title.strip()}"
                log_to_gui(
                    f"💾 [Alias Match] '{source_artist.strip()}' → '{alias_row[0]}' "
                    "(using cached alias for similarity)"
                )
        except sqlite3.Error as exc:
            log_to_gui(f"⚠️ [Alias Match] Cache read failed: {exc}")

    target_raw = clean_noise(similarity_query.lower())
    clean_target = "".join(ch for ch in unicodedata.normalize("NFKC", target_raw).casefold() if ch.isalnum())

    try:
        results = ytmusic.search(query, filter="songs")
        if not results:
            log_to_gui("⚠️ [YTMusicAPI] 'Songs' empty. Trying videos...")
            results = ytmusic.search(query, filter="videos")

        if not results:
            log_to_gui("❌ [YTMusicAPI] No results found.")
            return try_artist_alias() or (None, 0, "", "")

        for res in results[:5]:
            vid_id = res.get('videoId')
            if not vid_id: continue

            found_title = res.get('title', 'Unknown')
            artists_list = res.get('artists', [])
            found_artist = artists_list[0].get('name', '') if artists_list else ''

            duration_sec = 0
            if 'duration_seconds' in res:
                duration_sec = res.get('duration_seconds', 0)
            elif 'duration' in res:
                duration_sec = _parse_duration(res.get('duration'))

            full_found_string = f"{found_artist} - {found_title}" if found_artist else found_title
            found_raw  = clean_noise(full_found_string.lower())
            clean_found = "".join(ch for ch in unicodedata.normalize("NFKC", found_raw).casefold() if ch.isalnum())

            similarity = difflib.SequenceMatcher(None, clean_target, clean_found).ratio()
            
            title_lower = found_title.lower()
            if "official video" in title_lower or "music video" in title_lower:
                similarity = max(0.0, similarity - 0.30)
                log_to_gui(f"🔻 [VETO] Video penalty applied! Score: {similarity:.2f}")

            if similarity >= 0.70:
                log_to_gui(f"\n✅ [Match] '{found_artist} - {found_title}' (Sim: {similarity:.2f})\n")

                clean_ytm_title = clean_noise(found_title)
                ytm_key = f"{found_artist} - {clean_ytm_title}".strip()
                
                try:
                    with db_connect() as conn:
                        old_row = conn.execute(
                            "SELECT score, last_played, genre, year, description, listeners, tags, similar, track_tags, track_tag_checked_at FROM preferences WHERE item_name = ? AND item_type = 'track'",
                            (query_key,)
                        ).fetchone()

                        old_score, old_lp, old_genre, old_year, old_desc, old_list, old_tags, old_sim, old_track_tags, old_tags_checked = (
                            old_row if old_row else (10, 0, genre_info, 0, "", 0, "", "", "", 0)
                        )

                        effective_genre = old_genre if (old_genre and old_genre.strip().lower() not in ("unknown", "", "music")) else genre_info

                        if query_key.lower() != ytm_key.lower():
                            previous_vibes = conn.execute(
                                "SELECT vibe_key, vibe_score, interactions, last_updated "
                                "FROM vibe_feedback WHERE track_key=?", (query_key,)
                            ).fetchall()
                            for vibe_key, vibe_score, interactions, updated in previous_vibes:
                                existing_vibe = conn.execute(
                                    "SELECT vibe_score, interactions FROM vibe_feedback WHERE track_key=? AND vibe_key=?",
                                    (ytm_key, vibe_key),
                                ).fetchone()
                                if existing_vibe:
                                    total_n = int(existing_vibe[1]) + int(interactions)
                                    merged = (
                                        (float(existing_vibe[0]) * int(existing_vibe[1]) + float(vibe_score) * int(interactions)) / total_n
                                        if total_n else 50.0
                                    )
                                    conn.execute(
                                        "UPDATE vibe_feedback SET vibe_score=?, interactions=?, "
                                        "last_updated=MAX(last_updated,?) WHERE track_key=? AND vibe_key=?",
                                        (merged, total_n, updated, ytm_key, vibe_key),
                                    )
                                else:
                                    conn.execute(
                                        "INSERT INTO vibe_feedback(track_key,vibe_key,vibe_score,interactions,last_updated) "
                                        "VALUES (?,?,?,?,?)",
                                        (ytm_key, vibe_key, vibe_score, interactions, updated),
                                    )
                            conn.execute("DELETE FROM vibe_feedback WHERE track_key=?", (query_key,))
                            conn.execute("DELETE FROM preferences WHERE item_name = ? AND item_type = 'track'", (query_key,))
                            delete_from_chroma(query_key)
                            log_to_gui(f"🔄 [Migrate] '{query_key}' → '{ytm_key}'")

                        conn.execute(
                            "INSERT INTO preferences (item_name, item_type, score, last_played, genre, video_id, duration, year, description, listeners, tags, similar, track_tags, track_tag_checked_at) "
                            "VALUES (?, 'track', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?) "
                            "ON CONFLICT(item_name) DO UPDATE SET "
                            "video_id = excluded.video_id, duration = excluded.duration, "
                            "score = MAX(score, excluded.score), last_played = MAX(last_played, excluded.last_played), "
                            "genre = CASE WHEN preferences.genre IN ('', 'Unknown', 'Music') THEN excluded.genre ELSE preferences.genre END, "
                            "track_tags = CASE WHEN preferences.track_tags = '' THEN excluded.track_tags ELSE preferences.track_tags END, "
                            "track_tag_checked_at=MAX(preferences.track_tag_checked_at,excluded.track_tag_checked_at)",
                            (ytm_key, old_score, old_lp, effective_genre, vid_id, int(duration_sec), old_year, old_desc, old_list, old_tags, old_sim, old_track_tags, old_tags_checked)
                        )
                        sync_track_to_chroma(
                            ytm_key, "track", old_score, old_lp, effective_genre, vid_id, int(duration_sec), 
                            year=old_year, description=old_desc, listeners=old_list,
                            tags=old_tags, similar=old_sim, track_tags=old_track_tags
                        )

                except Exception as db_err:
                    log_to_gui(f"⚠ [DB Cache] Error: {db_err}")

                return vid_id, int(duration_sec), found_artist, clean_ytm_title

        log_to_gui("\n❌ [YTMusicAPI] No acceptable match.\n")
        return try_artist_alias() or (None, 0, "", "")

    except Exception as exc:
        log_to_gui(f"\n🚨 [YTMusicAPI] Critical: {exc}\n")
        return None, 0, "", ""


def update_nowplaying_file(artist: str, title: str):
    file_path = os.path.join(DATA_DIR, "nowplaying.txt")
    try:
        with open(file_path, "w", encoding="utf-8") as f:
            f.write(f"{artist} - {title}")
    except Exception as exc:
        print(f"Error writing nowplaying.txt: {exc}")


def _apply_skip_penalty(video_id: str | None, already_scored: bool, duration: float, elapsed: float):
    if getattr(cfg, 'PENALTY_APPLIED', False) or getattr(cfg, 'TRACK_FAILED', False):
        return
    if not video_id or already_scored or duration <= 0 or elapsed <= 5:
        return
    if (elapsed / duration * 100) < cfg.PENALTY_THRESHOLD_PERCENT:
        track_key = f"{cfg.CURRENT_ARTIST} - {cfg.CURRENT_TITLE}"
        active_vibe = (cfg.CURRENT_PLAY_VIBE or "").strip()
        user_queued = getattr(cfg, 'CURRENT_PLAY_SOURCE', 'judge') == 'user_queue'
        if active_vibe:
            penalty = cfg.USER_QUEUE_VIBE_PENALTY if user_queued else cfg.POINTS_PENALTY
            record_vibe_score(track_key, active_vibe, penalty)
            source_note = "user queue" if user_queued else "Judge"
            log_to_gui(
                f"📉 Vibe penalty ({penalty:+} pts) for '{track_key}' in [{active_vibe}] "
                f"[{source_note}] — global score untouched"
            )
        else:
            update_score(track_key, 'track', cfg.POINTS_PENALTY)
            log_to_gui(f"📉 Global skip penalty ({cfg.POINTS_PENALTY:+} pts) applied for '{track_key}'")
        cfg.PENALTY_APPLIED = True


def add_to_db_queue(item: str, push_to_front: bool = False, source: str = "user_queue"):
    if not item: return
    try:
        with db_connect() as conn:
            ts = 0 if push_to_front else time.time()
            conn.execute(
                "INSERT INTO queue (item, timestamp, source) VALUES (?, ?, ?)",
                (item.strip(), ts, source or "user_queue"),
            )
        start_thread(update_queue_ui)
    except Exception as e:
        print(f"⚠️ DB Queue Add Error: {e}")

def get_next_from_queue() -> str:
    cfg.LAST_DEQUEUED_SOURCE = ""
    try:
        with db_connect() as conn:
            row = conn.execute("SELECT rowid, item, source FROM queue ORDER BY timestamp ASC, rowid ASC LIMIT 1").fetchone()
            if row:
                queue_id, item, source = row
                conn.execute("DELETE FROM queue WHERE rowid = ?", (queue_id,))
                cfg.LAST_DEQUEUED_SOURCE = source or "user_queue"
                start_thread(update_queue_ui)
                return item
    except Exception as e:
        print(f"⚠️ DB Queue Fetch Error: {e}")
    return ""


def choose_next_track() -> tuple[str, str]:
    """Return (track, source), preserving whether the user or Judge made the choice."""
    queued = get_next_from_queue()
    if queued:
        return queued, (cfg.LAST_DEQUEUED_SOURCE or "user_queue")
    judged = ask_chroma_for_next_track()
    if judged:
        return judged, "judge"
    return get_fallback_track(), "fallback"


def _resolved_track_is_on_cooldown(artist: str, track_key: str) -> tuple[bool, str]:
    now = time.time()
    track_cutoff = now - cfg.TRACK_COOLDOWN_MINUTES * 60
    artist_cutoff = now - cfg.ARTIST_COOLDOWN_MINUTES * 60
    with db_connect() as conn:
        track_row = conn.execute(
            "SELECT last_played FROM preferences WHERE item_name=? COLLATE NOCASE AND item_type='track'",
            (track_key.strip(),),
        ).fetchone()
        if track_row and float(track_row[0] or 0) > track_cutoff:
            minutes_left = max(1, math.ceil((float(track_row[0]) + cfg.TRACK_COOLDOWN_MINUTES * 60 - now) / 60))
            return True, f"track lock ({minutes_left}m left)"

        artist_row = conn.execute(
            "SELECT last_played FROM preferences WHERE item_name=? COLLATE NOCASE AND item_type='artist'",
            (artist.strip(),),
        ).fetchone()
        if artist_row and float(artist_row[0] or 0) > artist_cutoff:
            minutes_left = max(1, math.ceil((float(artist_row[0]) + cfg.ARTIST_COOLDOWN_MINUTES * 60 - now) / 60))
            return True, f"artist lock ({minutes_left}m left)"

    return False, ""


def _choose_resolved_track(max_attempts: int = 12):
    for _ in range(max_attempts):
        query, source = choose_next_track()
        parts = query.split(" - ", 1)
        query_artist = parts[0].strip()
        query_title = parts[1].strip() if len(parts) > 1 else "?"
        genre = _get_track_or_artist_genre(query, query_artist)
        video_id, duration, ytm_artist, ytm_title = search_ytm_track(query, genre_info=genre)
        if not video_id:
            continue

        artist = ytm_artist if ytm_artist else query_artist
        title = ytm_title if ytm_title else query_title
        track_key = f"{artist} - {title}"

        if source != "user_queue":
            locked, reason = _resolved_track_is_on_cooldown(artist, track_key)
            if locked:
                log_to_gui(
                    f"🔒 [Cooldown Guard] Rejected resolved track '{track_key}': {reason}. Reselecting…"
                )
                continue

        return query, source, video_id, duration, artist, title, genre

    log_to_gui("🚨 [Cooldown Guard] Could not resolve an unlocked track after repeated attempts.")
    return None, None, None, 0, "", "", "Unknown"


def remove_from_db_queue(item: str):
    if not item: return
    try:
        with db_connect() as conn:
            row = conn.execute("SELECT rowid FROM queue WHERE item = ? ORDER BY timestamp ASC, rowid ASC LIMIT 1", (item.strip(),)).fetchone()
            if row:
                conn.execute("DELETE FROM queue WHERE rowid = ?", (row[0],))
        start_thread(update_queue_ui)
        log_to_gui(f"🗑 '{item}' deleted from the queue!")
    except Exception as e:
        print(f"⚠️ DB Queue Remove Error: {e}")


# =============================================================================
# TEMPORARY AUDIO PREFETCH
# ============================================================================
def _temp_audio_dir():
    path = os.path.join(DATA_DIR, "temp_audio")
    os.makedirs(path, exist_ok=True)
    return path


def _safe_audio_filename(track_key: str) -> str:
    return re.sub(r'[\\/*?:"<>|]', "", track_key).strip() or "spyder_temp"


class _PreloadCancelled(Exception):
    """Cooperative cancellation of yt-dlp transfers."""


def _ready_local_playback_file(source: str, track_key: str) -> str | None:
    """Use already available audio immediately; never route local hits via yt-dlp."""
    if not source:
        return None
    try:
        if os.path.isfile(source):
            return os.path.abspath(source)
        cached = os.path.join(DATA_DIR, "temp_audio", f"{_safe_audio_filename(track_key)}.webm")
        if os.path.isfile(cached) and os.path.getsize(cached) > 100000:
            return cached
    except OSError:
        pass
    return None


def _prepare_playback_file(source: str, track_key: str, cancel_event=None) -> str | None:
    if not source or (cancel_event is not None and cancel_event.is_set()):
        return None
    if os.path.exists(source):
        return os.path.abspath(source)

    safe_name = _safe_audio_filename(track_key)
    target = os.path.join(_temp_audio_dir(), f"{safe_name}.webm")
    if os.path.exists(target) and os.path.getsize(target) > 100000:
        log_to_gui(f"⚡ [Temp Cache] Ready: '{track_key}'")
        return target

    for stale in (target, target + ".part"):
        try:
            if os.path.exists(stale):
                os.remove(stale)
        except OSError:
            pass

    log_to_gui(f"📥 [Preload] Fetching '{track_key}' with yt-dlp...")

    # Native yt-dlp Python API, not a separate youtube-dl.exe / subprocess.
    # Keep the existing audio format preference, music client, cache filename
    # and return contract unchanged for the dual-deck player.
    try:
        from yt_dlp import YoutubeDL

        start_monotonic = time.monotonic()
        first_transfer_at = [None]
        last_transfer_at = [None]

        def _progress_hook(data):
            if cancel_event is not None and cancel_event.is_set():
                raise _PreloadCancelled("Crossfade deadline reached")
            if data.get('status') == 'downloading':
                now = time.monotonic()
                if first_transfer_at[0] is None:
                    first_transfer_at[0] = now
                last_transfer_at[0] = now

        options = {
            "progress_hooks": [_progress_hook],
            "noplaylist": True,
            "format": "ba[ext=webm]/ba",
            "extractor_args": {"youtube": {"player_client": ["tv_music"]}},
            "outtmpl": target,
            "quiet": True,
            "no_warnings": True,
            "noprogress": True,
        }
        with YoutubeDL(options) as ydl:
            if cancel_event is not None and cancel_event.is_set():
                raise _PreloadCancelled("Cancelled before transfer")
            result = ydl.download([f"https://music.youtube.com/watch?v={source}"])

        if cancel_event is not None and cancel_event.is_set():
            raise _PreloadCancelled("Cancelled after transfer")
        if result == 0 and os.path.exists(target) and os.path.getsize(target) > 100000:
            total_seconds = max(0.001, time.monotonic() - start_monotonic)
            transfer_seconds = (last_transfer_at[0] - first_transfer_at[0]
                                if first_transfer_at[0] is not None and last_transfer_at[0] is not None
                                else total_seconds)
            # A very short progress interval is not a reliable throughput sample.
            if transfer_seconds < 0.2:
                transfer_seconds = total_seconds
            _record_download_measurement(os.path.getsize(target), total_seconds, transfer_seconds)
            log_to_gui(f"✅ [Preload] Ready: '{track_key}'")
            return target
    except _PreloadCancelled:
        log_to_gui(f"🛑 [Preload] Cancelled late download: {track_key}")
    except ImportError as exc:
        log_to_gui(f"⚠️ [Preload] yt-dlp Python package missing: {exc}")
    except Exception as exc:
        print(f"Preload error: {exc}")

    # A cancelled transfer must never leave a playable-looking partial file.
    if cancel_event is not None and cancel_event.is_set():
        for suffix in ("", ".part", ".ytdl"):
            try:
                os.remove(target + suffix)
            except FileNotFoundError:
                pass
            except OSError:
                pass
        return None
    log_to_gui(f"⚠️ [Preload] Download failed for '{track_key}'.")
    return None


def _finish_temp_playback(path: str, track_key: str, delay: float = 0.0):
    if not path:
        return
    temp_root = os.path.abspath(_temp_audio_dir())
    abs_path = os.path.abspath(path)
    try:
        if os.path.commonpath([temp_root, abs_path]) != temp_root:
            return
    except ValueError:
        return

    def worker():
        if delay > 0:
            time.sleep(delay)
        if not os.path.exists(abs_path):
            return
        try:
            if getattr(cfg, 'AUTO_DOWNLOAD', False):
                cache_dir = os.path.join(DATA_DIR, "cache")
                os.makedirs(cache_dir, exist_ok=True)
                final_path = os.path.join(cache_dir, f"{_safe_audio_filename(track_key)}.webm")
                os.replace(abs_path, final_path)
                with db_connect() as conn:
                    conn.execute(
                        "UPDATE preferences SET local = 1 WHERE item_name = ?",
                        (track_key,),
                    )
                log_to_gui(f"🔒 Safe! '{track_key}' kept in local cache.")
            else:
                os.remove(abs_path)
                log_to_gui(f"🧹 [Temp] Removed '{track_key}'.")
        except Exception as exc:
            print(f"Temp finalize error: {exc}")

    start_thread(worker)


# =============================================================================
# MAIN DJ ENGINE (UNCHANGED FROM SPYDER_ENGINE_2.PY)
# =============================================================================
def run_dj_logic():
    # Database is initialized by SpyderMUSIC.py before the Qt GUI starts.
    while cfg.RUNNING:
        cfg.DJ_ACTIVATED = False
        log_to_gui("📡 SPYDERMUSIC ENGINE READY – Standing by for Play...")

        while cfg.RUNNING:
            if getattr(cfg, 'DJ_ACTIVATED', False):
                break
            time.sleep(0.5)

        if not cfg.RUNNING:
            return
        if get_playback_policy().local_only:
            cfg.DJ_ACTIVATED = False
            log_to_gui('ℹ️ Local mode: Auto DJ startup blocked; use a local playlist.')
            continue

        log_to_gui("🚀 DJ ACTIVATED: Initializing first track...")

        (next_query, current_source, current_video_id, current_duration,
         resolved_artist, resolved_title, current_genre) = _choose_resolved_track()

        if not current_video_id:
            log_to_gui("⚠️ DJ Error: Initial track search/cooldown resolution failed. Retrying...")
            cfg.DJ_ACTIVATED = False
            continue

        cfg.CURRENT_ARTIST = resolved_artist
        cfg.CURRENT_TITLE  = resolved_title
        cfg.CURRENT_VIDEO_ID = current_video_id
        
        track_key = f"{cfg.CURRENT_ARTIST} - {cfg.CURRENT_TITLE}"
        cfg.CURRENT_PLAY_VIBE = cfg.SELECTED_PROMPT_PREF.strip()
        cfg.CURRENT_PLAY_SOURCE = current_source
        cfg.ORIGINAL_DB_SCORE = log_playback_and_get_score(cfg.CURRENT_ARTIST, track_key)

        current_play_target = _prepare_playback_file(current_video_id, track_key)
        if not current_play_target:
            log_to_gui("⚠️ DJ Error: Initial audio preload failed. Retrying...")
            cfg.DJ_ACTIVATED = False
            continue
        mpv_player.play(current_play_target, current_duration)
        record_judge_rotation(track_key, current_source)
        update_nowplaying_file(cfg.CURRENT_ARTIST, cfg.CURRENT_TITLE)
        log_to_gui(f"\n🎵 NOW PLAYING: {cfg.CURRENT_ARTIST} - {cfg.CURRENT_TITLE}")
        log_to_gui(f"📂 Genre: {current_genre}")

        cfg.MANUAL_SCORE_GIVEN = False
        cfg.CURRENT_PENDING_SCORE = cfg.ORIGINAL_DB_SCORE

        if hasattr(gui, 'lbl_current_score') and gui.root:
            gui.root.after(
                0,
                lambda s=cfg.CURRENT_PENDING_SCORE: gui.lbl_current_score.configure(text=f"{s} Pts"),
            )

        triggered_next = False
        scored = False
        next_video_id = None
        next_duration = 0
        next_artist = ""
        next_title = ""
        next_genre = ""
        next_source = "judge"
        next_play_target = None
        pending_download = None
        pending_track_key = None
        pending_cancel = None
        retry_after_deadline = False
        failed_preloads = set()

        while cfg.RUNNING and getattr(cfg, 'DJ_ACTIVATED', True):
            
            # --- 1. HANDLE MANUAL FADE STOP ---
            if getattr(cfg, 'FORCE_FADE_STOP', False):
                cfg.FORCE_FADE_STOP = False
                cfg.DJ_ACTIVATED = False
                log_to_gui("🛑 [DJ Core] Manual fade-out complete. Engine in standby.")
                _finish_temp_playback(current_play_target, track_key, delay=1.0)
                break

            if not mpv_player.is_playing() and cfg.RUNNING and not retry_after_deadline:
                # A stopped/EOF deck cannot advance to the normal crossfade
                # threshold. Stop After must also complete at natural EOF.
                if getattr(cfg, 'STOP_AFTER_CURRENT', False) and mpv_player.playback_started:
                    log_to_gui("\n🛑 [DJ Core] Stop-after-current reached natural EOF.")
                    mpv_player.stop()
                    _finish_temp_playback(current_play_target, track_key, delay=1.0)
                    cfg.STOP_AFTER_CURRENT = False
                    cfg.DJ_ACTIVATED = False
                    break
                time.sleep(1)
                continue

            elapsed = mpv_player.get_elapsed()

            # Repeat One is a regular deck transition, never an internal mpv loop.
            # If Repeat was switched off while the same song was preloaded, allow
            # the ordinary next-track selection to replace that idle deck.
            if not cfg.REPEAT_TRACK and next_source == 'repeat_one' and triggered_next:
                triggered_next = False
                next_video_id = None
                next_play_target = None
                next_source = 'judge'


            def _award_current_track_if_due(played_seconds):
                """Award at most once, after the score is successfully stored."""
                nonlocal scored
                if scored or current_duration <= 0 or getattr(cfg, 'TRACK_FAILED', False):
                    return
                if played_seconds / current_duration * 100 <= getattr(cfg, 'SCORE_THRESHOLD_PERCENT', 60):
                    return
                try:
                    track_key = f"{cfg.CURRENT_ARTIST} - {cfg.CURRENT_TITLE}"
                    delta = (
                        cfg.CURRENT_PENDING_SCORE - cfg.ORIGINAL_DB_SCORE
                        if getattr(cfg, 'MANUAL_SCORE_GIVEN', False)
                        else getattr(cfg, 'POINTS_WIN', 10)
                    )
                    active_vibe = (cfg.CURRENT_PLAY_VIBE or "").strip()
                    if getattr(cfg, 'MANUAL_SCORE_GIVEN', False):
                        update_score(cfg.CURRENT_ARTIST, 'artist', delta)
                        update_score(track_key, 'track', delta)
                        log_to_gui(f"💾 {track_key} global score adjusted ({delta:+} pts)")
                    elif active_vibe:
                        user_queued = getattr(cfg, 'CURRENT_PLAY_SOURCE', 'judge') == 'user_queue'
                        vibe_delta = cfg.USER_QUEUE_VIBE_WIN if user_queued else delta
                        if not record_vibe_score(track_key, active_vibe, vibe_delta):
                            return False
                        if user_queued:
                            update_score(cfg.CURRENT_ARTIST, 'artist', cfg.USER_QUEUE_GLOBAL_BONUS)
                            update_score(track_key, 'track', cfg.USER_QUEUE_GLOBAL_BONUS)
                            log_to_gui(
                                f"📌 User Queue learned: '{track_key}' {vibe_delta:+} vibe pts in [{active_vibe}] "
                                f"+ {cfg.USER_QUEUE_GLOBAL_BONUS:+} global pts"
                            )
                        else:
                            log_to_gui(
                                f"🎯 Vibe learned: '{track_key}' {vibe_delta:+} pts in [{active_vibe}] "
                                "— global score untouched"
                            )
                    else:
                        user_queued = getattr(cfg, 'CURRENT_PLAY_SOURCE', 'judge') == 'user_queue'
                        global_delta = delta + (cfg.USER_QUEUE_GLOBAL_BONUS if user_queued else 0)
                        update_score(cfg.CURRENT_ARTIST, 'artist', global_delta)
                        update_score(track_key, 'track', global_delta)
                        source_note = " [user queue]" if user_queued else ""
                        log_to_gui(f"💾 {track_key} archived ({global_delta:+} global pts){source_note}")
                    scored = True
                except Exception as exc:
                    log_to_gui(f"⚠️ Vibe scoring failed for {track_key}: {exc}")

            # --- 2. SCORING AT THRESHOLD ---
            _award_current_track_if_due(elapsed)

            # --- 3. PRE-FETCH AND PRE-BUFFER ---
            time_remaining = (current_duration - elapsed) if current_duration > 0 else 999
            advance_trigger_sec = max(
                getattr(cfg, 'CROSSFADE_SEC', 5) + 5,
                getattr(cfg, 'CROSSFADE_SEC', 5) + 5 + _estimated_download_seconds(),
            )
            
            # A manual skip must pick its replacement now, regardless of the
            # automatic download forecast and its remaining-time threshold.
            if (getattr(cfg, 'FORCE_SKIP', False) or 0 < time_remaining <= advance_trigger_sec) and not triggered_next:
                if (getattr(cfg, 'REPEAT_TRACK', False)
                        and not getattr(cfg, 'FORCE_SKIP', False)
                        and not getattr(cfg, 'STOP_AFTER_CURRENT', False)):
                    # Repeat the current audio on the idle deck. The same
                    # transition/crossfade/PCM restart handles normal tracks.
                    if mpv_player.load_next(current_play_target):
                        triggered_next = True
                        next_video_id = current_video_id
                        next_play_target = current_play_target
                        next_duration = current_duration
                        next_artist, next_title = cfg.CURRENT_ARTIST, cfg.CURRENT_TITLE
                        next_genre = current_genre
                        next_source = 'repeat_one'
                        log_to_gui('🔂 [Repeat One] Current track prepared on the other deck.')
                else:
                    triggered_next = True

                    if getattr(cfg, 'STOP_AFTER_CURRENT', False):
                        log_to_gui("🛑 [DJ Core] Stop pending. Skipping pre-buffer.")
                        next_video_id = None
                    else:
                        ai_log("🔄 Preparing next track sequence...\n")

                        manual_next = getattr(cfg, 'MANUAL_NEXT_TRACK', "")
                        if manual_next:
                            new_query, next_source = manual_next, "user_queue"
                            cfg.MANUAL_NEXT_TRACK = ""
                            n_parts = new_query.split(" - ", 1)
                            q_artist = n_parts[0].strip()
                            q_title = n_parts[1].strip() if len(n_parts) > 1 else "?"
                            next_genre = _get_track_or_artist_genre(new_query, q_artist)
                            next_video_id, next_duration, ytm_a, ytm_t = search_ytm_track(new_query, genre_info=next_genre)
                            next_artist = ytm_a if ytm_a else q_artist
                            next_title = ytm_t if ytm_t else q_title
                        else:
                            cfg.MANUAL_NEXT_TRACK = ""
                            (new_query, next_source, next_video_id, next_duration,
                             next_artist, next_title, next_genre) = _choose_resolved_track()
                        if next_video_id:
                            next_track_key = f"{next_artist} - {next_title}"
                            pending_track_key = next_track_key
                            local_target = _ready_local_playback_file(next_video_id, next_track_key)
                            if local_target is not None:
                                # Local tracks must be ready for a manual skip in
                                # this very loop iteration, not a future tick.
                                if mpv_player.load_next(local_target):
                                    next_play_target = local_target
                                    log_to_gui(f"⏭ Pre-buffered locally: {next_artist} - {next_title}")
                                    log_to_gui(f"📂 Genre: {next_genre}\n")
                                else:
                                    failed_preloads.add(next_video_id)
                                    next_video_id = None
                                    next_play_target = None
                            else:
                                # Only actual downloads belong in the worker pool.
                                pending_cancel = threading.Event()
                                pending_download = _AUDIO_PRELOAD_POOL.submit(
                                    _prepare_playback_file, next_video_id, next_track_key, pending_cancel
                                )
                        else:
                            log_to_gui("⚠️ [DJ Core] Pre-buffer search failed. Transition will fall back.")

            # Load the completed download into the idle MPV deck.
            if pending_download is not None and pending_download.done():
                try:
                    next_play_target = pending_download.result()
                except Exception as exc:
                    log_to_gui(f"⚠️ [Preload] Worker failed: {exc}")
                    next_play_target = None
                pending_download = None
                pending_cancel = None
                if next_play_target and mpv_player.load_next(next_play_target):
                    log_to_gui(f"⏭ Pre-buffered locally: {next_artist} - {next_title}")
                    log_to_gui(f"📂 Genre: {next_genre}\n")
                else:
                    failed_preloads.add(next_video_id)
                    next_video_id = None
                    next_play_target = None
                    log_to_gui("⚠️ [DJ Core] Preload unavailable; selecting another track.")

            # --- 4. TRACK TRANSITION & STOP AFTER CURRENT ---
            force_skip = getattr(cfg, 'FORCE_SKIP', False)
            current_fade_time = getattr(cfg, 'CROSSFADE_SEC', 5)

            if force_skip:
                _apply_skip_penalty(current_video_id, scored, current_duration, elapsed)

            # Stop After is not a crossfade: let the song reach its actual end.
            stop_after_pending = bool(getattr(cfg, 'STOP_AFTER_CURRENT', False))
            end_threshold = 0.0 if stop_after_pending else current_fade_time
            is_end_of_track = (current_duration > 0 and elapsed >= (current_duration - end_threshold))

            if (is_end_of_track and cfg.REPEAT_TRACK and not force_skip
                    and not stop_after_pending and next_source != 'repeat_one'):
                if mpv_player.load_next(current_play_target):
                    next_video_id = current_video_id
                    next_play_target = current_play_target
                    next_duration = current_duration
                    next_artist, next_title = cfg.CURRENT_ARTIST, cfg.CURRENT_TITLE
                    next_genre = current_genre
                    next_source = 'repeat_one'
                    triggered_next = True

            if force_skip or is_end_of_track:
                # Final check before overwriting the previous track's scoring state.
                # Never award a deliberately skipped track here.
                if not force_skip:
                    _award_current_track_if_due(elapsed)

                if getattr(cfg, 'STOP_AFTER_CURRENT', False):
                    log_to_gui("\n🛑 [DJ Core] Stop-after-current triggered. Halting engine.")
                    mpv_player.stop()
                    _finish_temp_playback(current_play_target, track_key, delay=1.0)
                    cfg.STOP_AFTER_CURRENT = False
                    cfg.FORCE_SKIP = False
                    cfg.DJ_ACTIVATED = False
                    break

                if pending_download is not None and retry_after_deadline:
                    # The replacement download may complete on a later tick.
                    # Keep the skip request until playback really changes.
                    time.sleep(0.2)
                    continue

                if pending_download is not None:
                    # At a real crossfade deadline, use local audio instead of
                    # waiting for yt-dlp. A manual skip also prefers a ready
                    # local alternative, but need not cancel a download if
                    # absolutely nothing is available yet.
                    local = _choose_local_audio_fallback(pending_track_key or '') if force_skip else None
                    if force_skip and local is None:
                        retry_after_deadline = True
                        time.sleep(0.2)
                        continue

                    if pending_cancel is not None:
                        pending_cancel.set()
                    pending_download.cancel()
                    failed_preloads.add(next_video_id)
                    pending_download = None
                    pending_cancel = None
                    next_video_id = None
                    next_play_target = None
                    if force_skip:
                        log_to_gui("⏭ [Skip] Replacing unfinished download with available local audio.")
                    else:
                        log_to_gui("🛑 [Preload] Crossfade deadline: cancelling late download.")
                        local = _choose_local_audio_fallback(pending_track_key or '')

                    if local is not None:
                        local_path, local_duration, local_artist, local_title, local_genre = local
                        if mpv_player.load_next(local_path):
                            next_video_id = local_path
                            next_play_target = local_path
                            next_duration = local_duration
                            next_artist, next_title, next_genre = local_artist, local_title, local_genre
                            next_source = 'judge'
                            log_to_gui(f"🛟 [Fallback] Local track selected: {local_artist} - {local_title}")
                    if not next_video_id:
                        retry_after_deadline = True
                        log_to_gui("🔄 [Fallback] No local audio; retrying with a fresh download.")

                if not next_video_id:
                    selected = None
                    for _ in range(12):
                        candidate = _choose_resolved_track()
                        if candidate[2] and candidate[2] not in failed_preloads:
                            selected = candidate
                            break
                    if selected is None:
                        log_to_gui("⚠️ [Fallback] No fresh candidate; retrying shortly.")
                        retry_after_deadline = True
                        time.sleep(1)
                        continue
                    (raw_query, next_source, next_video_id, next_duration,
                     next_artist, next_title, next_genre) = selected
                    pending_track_key = f"{next_artist} - {next_title}"
                    local_target = _ready_local_playback_file(next_video_id, pending_track_key)
                    if local_target is not None:
                        # The selected Judge result is already on disk. Do not
                        # call this a new download or defer the manual skip.
                        if not mpv_player.load_next(local_target):
                            failed_preloads.add(next_video_id)
                            next_video_id = None
                            next_play_target = None
                            time.sleep(0.2)
                            continue
                        next_play_target = local_target
                        retry_after_deadline = False
                        log_to_gui(f"⏭ Pre-buffered locally: {next_artist} - {next_title}")
                        log_to_gui(f"📂 Genre: {next_genre}\n")
                    else:
                        pending_cancel = threading.Event()
                        pending_download = _AUDIO_PRELOAD_POOL.submit(
                            _prepare_playback_file, next_video_id, pending_track_key, pending_cancel
                        )
                        next_play_target = None
                        retry_after_deadline = True
                        log_to_gui(f"📥 [Fallback] New attempt: {pending_track_key}")
                        time.sleep(0.2)
                        continue
                if next_play_target is not None:
                    log_to_gui(f"🔀 Crossfading ({current_fade_time}s)...")
                    if not mpv_player.trigger_transition(next_duration, crossfade_sec=current_fade_time):
                        log_to_gui("⚠️ [DJ Core] Crossfade deck not ready; falling back to direct play.")
                        mpv_player.stop()
                        if not mpv_player.play(next_play_target, next_duration):
                            cfg.TRACK_FAILED = True
                            cfg.FORCE_SKIP = True
                            next_video_id = None
                            next_play_target = None
                            triggered_next = False
                            continue
                cfg.FORCE_SKIP = False

                previous_play_target = current_play_target
                previous_track_key = track_key
                if previous_play_target != next_play_target:
                    _finish_temp_playback(
                        previous_play_target,
                        previous_track_key,
                        delay=current_fade_time + 1.0,
                    )

                current_video_id, current_duration = next_video_id, next_duration
                current_play_target = next_play_target
                cfg.CURRENT_ARTIST, cfg.CURRENT_TITLE = next_artist, next_title
                cfg.CURRENT_VIDEO_ID = current_video_id
                cfg.CURRENT_PLAY_SOURCE = next_source

                cfg.TRACK_FAILED = False
                cfg.PENALTY_APPLIED = False

                if current_video_id:
                    log_to_gui(f"\n🎵 NOW PLAYING: {cfg.CURRENT_ARTIST} - {cfg.CURRENT_TITLE}")
                    update_nowplaying_file(cfg.CURRENT_ARTIST, cfg.CURRENT_TITLE)

                    track_key = f"{cfg.CURRENT_ARTIST} - {cfg.CURRENT_TITLE}"
                    if next_source != 'repeat_one':
                        record_judge_rotation(track_key, next_source)
                    now_playing_genre = _get_track_or_artist_genre(track_key, cfg.CURRENT_ARTIST)
                    log_to_gui(f"📂 Genre: {now_playing_genre}")

                    cfg.CURRENT_PLAY_VIBE = cfg.SELECTED_PROMPT_PREF.strip()
                    cfg.ORIGINAL_DB_SCORE = log_playback_and_get_score(cfg.CURRENT_ARTIST, track_key)
                    cfg.MANUAL_SCORE_GIVEN = False
                    cfg.SONG_COUNTER = getattr(cfg, 'SONG_COUNTER', 0) + 1
                    cfg.CURRENT_PENDING_SCORE = cfg.ORIGINAL_DB_SCORE

                    if hasattr(gui, 'lbl_current_score') and gui.root:
                        gui.root.after(
                            0, lambda s=cfg.CURRENT_PENDING_SCORE: gui.lbl_current_score.configure(text=f"{s} Pts")
                        )

                triggered_next = False
                scored = False
                next_video_id = None
                next_play_target = None
                pending_download = None
                pending_track_key = None
                pending_cancel = None
                retry_after_deadline = False
                failed_preloads.clear()

            time.sleep(1)


# =============================================================================
# MANUAL CONTROLS
# =============================================================================
def manual_score_current(points: int):
    cfg.CURRENT_PENDING_SCORE += points
    cfg.MANUAL_SCORE_GIVEN = True
    if gui.lbl_current_score is not None:
        gui.lbl_current_score.configure(text=f"{cfg.CURRENT_PENDING_SCORE} Pts")
    sign = "+" if points > 0 else ""
    log_to_gui(f"🎯 Score adjusted: {sign}{points} (pending total: {cfg.CURRENT_PENDING_SCORE} pts)")


def _manual_ai_skip_worker():
    log_to_gui("🧠 Manually triggering ChromaDB for next track…")
    if gui.btn_ai_skip is not None:
        gui.btn_ai_skip.configure(state="disabled")
    try:
        next_track = ask_chroma_for_next_track()
        if next_track:
            add_to_db_queue(next_track, push_to_front=True, source="judge")
            cfg.FORCE_SKIP = True
        else:
            log_to_gui("⚠️ ChromaDB could not select a track.")
    finally:
        if gui.btn_ai_skip is not None:
            gui.btn_ai_skip.configure(state="normal")


def manual_ai_skip():
    start_thread(_manual_ai_skip_worker)


def active_playback_source() -> str:
    """Determine the source that owns playback, independently of selected mode."""
    controller = _offline_controller
    if controller is not None and controller.status()['active']:
        return 'local'
    if getattr(cfg, 'DJ_ACTIVATED', False):
        return 'auto_dj'
    return 'idle'


def remote_control(action: str):
    try:
        source = active_playback_source()
        if source == 'local':
            controller = get_offline_controller()
            if action == 'next':
                controller.next()
            elif action == 'previous':
                controller.previous()
            elif action in ('play-pause', 'toggle-play', 'toggle-pause'):
                mpv_player.toggle_pause()
            elif action == 'pause' and not mpv_player.is_paused:
                mpv_player.toggle_pause()
            elif action == 'play' and mpv_player.is_paused:
                mpv_player.toggle_pause()
            elif action == 'stop':
                controller.stop()
            return
        if action == 'stop':
            # Immediate stop of the active DJ playback, not a fade or stop-after.
            cfg.DJ_ACTIVATED = False
            cfg.FORCE_SKIP = False
            cfg.FORCE_FADE_STOP = False
            cfg.STOP_AFTER_CURRENT = False
            mpv_player.stop()
            log_to_gui('⏹ Playback stopped.')
        elif action == 'next':
            cfg.FORCE_SKIP = True
        elif action in ('play-pause', 'toggle-play', 'toggle-pause', 'play', 'pause'):
            if not getattr(cfg, 'DJ_ACTIVATED', False):
                if get_playback_policy().local_only:
                    log_to_gui('ℹ️ Local mode: choose a local playlist to play.')
                    return
                cfg.DJ_ACTIVATED = True
                log_to_gui('▶ Play pressed: Starting Engine...')
            elif action in ('play-pause', 'toggle-play', 'toggle-pause') or (action == 'pause' and not mpv_player.is_paused) or (action == 'play' and mpv_player.is_paused):
                mpv_player.toggle_pause()
                log_to_gui('⏯️ Play/Pause')
        elif action == 'previous':
            mpv_player.seek_absolute(0)
            log_to_gui('⏪ RESTART: Track reset to beginning')
    except Exception as exc:
        print(f'⚠️ MPV control error ({action}): {exc}')


def fade_stop_active_playback():
    """Fade the active deck, then retire local playlist ownership if applicable."""
    source = active_playback_source()
    if source == 'local':
        controller = get_offline_controller()
        # Stop playlist advancement immediately, but keep the MPV fade running.
        controller.deactivate()
        cfg.FORCE_FADE_STOP = False
        def finished():
            controller.stop()
            cfg.STOP_AFTER_CURRENT = False
            cfg.FORCE_FADE_STOP = False
            log_to_gui('[Local] Fade stop completed.')
        mpv_player.fade_out_and_stop(cfg.CROSSFADE_SEC, on_complete=finished)
    else:
        mpv_player.fade_out_and_stop(cfg.CROSSFADE_SEC)



def _enqueue_track_by_name(track: str):
    log_to_gui(f"🔄 Override: '{track}'…")
    add_to_db_queue(track, push_to_front=True)
    cfg.FORCE_SKIP = False


# Playback modes. Mode changes are non-destructive: active playback is not stopped.
def get_playback_policy():
    return policy_for(cfg.PLAYBACK_MODE,
                      offline_smart_selection=cfg.OFFLINE_SMART_SELECTION,
                      offline_enrichment=cfg.OFFLINE_ENRICHMENT)


def set_playback_mode(mode: str, *, smart_selection=None, enrichment=None):
    if mode not in VALID_MODES:
        raise ValueError(f'Invalid playback mode: {mode!r}')
    cfg.PLAYBACK_MODE = mode
    cfg.OFFLINE_SMART_SELECTION = False
    cfg.OFFLINE_ENRICHMENT = False
    cfg.save_to_ini()
    log_to_gui(f'🎛️ Playback mode: {mode} (takes effect on next selection)')
    return get_playback_policy()


# Central selection policy: both UI controls use these methods.
def get_playback_selection():
    return from_legacy(cfg.PLAYBACK_MODE, cfg.OFFLINE_SMART_SELECTION)

def set_playback_selection(selection):
    mode = legacy_mode(selection)
    return set_playback_mode(mode)

# Offline playlist bridge. UI selection will be wired in Step 05.
_offline_controller = None

def _on_local_track(track: dict):
    """Publish local tracks through the existing GUI now-playing pipeline."""
    cfg.CURRENT_ARTIST = track.get('artist') or 'Unknown'
    cfg.CURRENT_TITLE = track.get('title') or ''
    cfg.CURRENT_VIDEO_ID = ''
    update_nowplaying_file(cfg.CURRENT_ARTIST, cfg.CURRENT_TITLE)

def get_offline_controller():
    global _offline_controller
    if _offline_controller is None:
        _offline_controller = OfflinePlaybackController(
            mpv_player, db_connect,
            crossfade_seconds=lambda: getattr(cfg, 'CROSSFADE_SEC', 5),
            on_track=_on_local_track,
            repeat_enabled=lambda: bool(cfg.REPEAT_TRACK),
            stop_after_enabled=lambda: bool(cfg.STOP_AFTER_CURRENT),
            on_stop_after=lambda: setattr(cfg, 'STOP_AFTER_CURRENT', False),
        )
    return _offline_controller

def play_local_playlist(playlist_id: int, start_index: int = 0):
    """Start local-only playback; refuse to race the running Auto DJ."""
    if getattr(cfg, 'DJ_ACTIVATED', False):
        raise RuntimeError('Stop Auto DJ before starting an offline playlist')
    controller = get_offline_controller()
    controller.load_playlist(playlist_id)
    return controller.play(start_index)

def next_local_playlist_track():
    return get_offline_controller().next()

def stop_local_playlist():
    get_offline_controller().stop()
