# SpyderMUSIC Features 🕷️🎵

**Discover SpyderMUSIC.**

SpyderMUSIC combines an intelligent Auto DJ, a dedicated local music player, a shared libmpv audio engine, and an extensively customizable interface. Its discovery, selection, playback, and visual presentation systems are designed as separate parts of one application.

This document describes the features identified in the SpyderMUSIC V4 source and development inventory. It is a feature overview, not an installation guide or a guarantee that every optional integration is available without configuration.

## 1. Interface, Themes & Visual Design

### Visual Theme Editor

- Visually arrange, move, and resize interface elements, including precise positioning and resize handles.
- Use alignment guides, multi-selection, copy/paste, and undo/redo while editing.
- Organize designs with layers, including ordering, visibility, locking, and naming.
- Customize controls, panels, artwork, seek bars, meters, and visualizer elements.
- Assign custom image assets and configure their appearance and interaction states.
- Save themes and create new variants without editing the application source.

### QSS Studio

- Edit Qt stylesheet properties through a visual interface or directly in QSS.
- Preview interface styling while adjusting colors, typography, borders, and control states.
- Customize playlists, queues, menus, tabs, transport controls, sliders, and other interface components.
- Style supported audio-related panels and controls, including the parametric EQ interface.

### Theme Rendering & Visualizations

- GPU-assisted visual composition and theme rendering.
- Support for PNG, WebP, and animated visual assets where supported by the theme system.
- Configurable artwork presentation, including shaped covers and animated vinyl-style covers.
- Audio-reactive elements, FFT/spectrum displays, waveform-based visuals, and stereo VU meters.
- Multiple seek-bar presentations, including interactive linear and circular designs.
- Theme-driven transitions, gradients, borders, shadows, and glow effects.
- Theme import and reusable theme assets.

## 2. Auto DJ, Scout & Judge

### Intelligent Auto DJ

- Automatically select music based on genre, vibe, musical similarity, and learned listening preferences.
- Search a semantic music index backed by ChromaDB.
- Evaluate multiple candidates rather than simply playing the highest-scoring track.
- Balance familiar choices with exploration using a configurable selection temperature and weighted candidate selection.
- Apply artist and track cooldowns, exclusions, and fallback strategies.
- Keep a detailed AI decision log for inspecting selection behavior.

### Scout: Music Discovery

- Discover new music and expand the available collection for Auto DJ playback.
- Use existing music preferences and metadata as starting points for discovery.
- Use **AI Training** to seed discovery with selected music from a playlist.
- Support discovery centered on a **single reference track** selected through the queue, rather than requiring the entire listening history as the starting point.
- Enrich the music database with external metadata and tags, including Last.fm information.
- Integrate SpyderTRACK research results into the available track information.
- Use YouTube Music search and yt-dlp-based retrieval in the Auto DJ workflow.

### AI Training

- Scan a YouTube Music playlist by URL and display its artists and tracks.
- Select one or more tracks and assign a user-defined genre tag.
- Store selected artists and tracks as preferences in SQLite.
- Give new preference entries an initial positive score while retaining existing scores when updating their genre.
- Synchronize training entries with ChromaDB.
- Optionally append scanned playlist tracks to the playback queue or replace its contents.
- Run playlist scanning and training-data saving off the GUI thread.

**AI Training is a starting point for Scout discovery.** It is distinct from the Judge's ongoing feedback-based learning.

### Judge: Selection & Learning

- Score candidates using musical fit, genre similarity, personal preference, and available metadata.
- Apply different weighting strategies when a vibe is selected and when no vibe is active.
- Maintain both global preference scores and separate **track × vibe** feedback.
- Learn from listening behavior, including sufficient playback, early skips, and explicit queue choices.
- Treat manually queued tracks as a different signal from tracks selected by the Judge.
- Incorporate Last.fm tags and SpyderTRACK evidence when available.
- Display learned track/vibe relationships and allow individual vibe scores to be reset.
- Preserve exploration through weighted selection instead of rigidly replaying only the highest-rated tracks.

### Artist Rotation

- Track how often the Judge has selected individual songs by an artist.
- Detect uneven rotation across that artist's eligible tracks.
- Use a weighted lottery to favor underplayed songs while retaining musical suitability checks.
- Respect cooldowns, exclusions, and vibe compatibility.
- Keep user-queued plays separate from Judge rotation statistics.

## 3. Local Player & Playlist Management

### Dedicated Local Player

- Play local audio files without running Scout discovery or the Auto DJ selection policy.
- Use the **same libmpv audio engine** as Auto DJ, rather than a separate player.
- Start playback from a selected position in a playlist.
- Advance automatically through available tracks.
- Use the shared crossfade system for transitions between local tracks.
- Skip missing or unavailable audio files during playback.
- Determine missing track durations with FFprobe or, when available, Mutagen, and store recovered metadata.

### Persistent Playlists

- Create, rename, duplicate, and delete multiple local playlists.
- Add or remove individual tracks or multiple selections.
- Import audio files or entire folders, including drag-and-drop support.
- Reorder tracks by dragging or moving them up and down.
- Shuffle the stored playlist order and undo the last sorting operation.
- Keep playlist contents and ordering in SQLite.
- Allow the same audio track to appear in multiple playlists, or more than once in one playlist.
- Identify missing files without silently deleting playlist entries.
- Delete a playlist **without deleting the underlying audio files**.

### Playlist Import & Export

- Import local **M3U** and **M3U8** playlists.
- Export playlists as UTF-8 **M3U8** files.
- Prefer relative paths in exported playlists when possible.
- Register imported local audio in the music library.
- Handle common local audio formats, including MP3, FLAC, Opus, Ogg, WAV, M4A, AAC, WMA, and WebM.

### Local Queue & Playback Controls

- Double-click a playlist track to start playback.
- Play selected tracks immediately, schedule them next, or append them to a separate local queue.
- Prioritize local queue entries over the playlist's remaining playback order.
- Insert tracks from other playlists, then continue the original playlist.
- Enable playback shuffle **without changing the saved playlist order**.
- Navigate backward using playback history; restart the current track when no earlier entry is available.
- Support Repeat One and Stop After in the local playback controller.
- Show track information, including artist, title, duration, path, and availability.

## 4. Shared Audio Engine & Playback Modes

### libmpv Playback

- One shared libmpv-based playback engine for Auto DJ and Local Player.
- Two playback decks for preparing the next track and performing crossfades.
- Configurable crossfade duration and smooth transitions.
- Playback controls for play, pause, stop, seeking, next/previous, and supported fade/stop behaviors.
- Repeat One handled through the playback system without requiring a separate audio engine.
- Audio analysis data for spectrum visualizations, reactive effects, and stereo level meters.
- Playback monitoring and recovery paths for failed track loads.

### Two Clear Modes

| Capability | Auto DJ | Local Player |
| --- | --- | --- |
| Music selection | Judge-driven | Playlist- and queue-driven |
| Scout discovery | Enabled | Disabled |
| Automatic metadata enrichment | Enabled | Disabled by the current mode policy |
| Local audio playback | Supported | Supported |
| Music downloads | Available through discovery workflow | Not part of local playback |
| Audio engine | Shared libmpv engine | Shared libmpv engine |

Switching modes changes **future track selection** without forcibly interrupting the track already playing.

## 5. Music Data & Architecture

- **SQLite** stores preferences, track metadata, queues, local library entries, and playlists.
- **ChromaDB** supports semantic matching and is isolated in a separate process to reduce interference with GUI responsiveness.
- Background workers handle selected database, discovery, and metadata tasks.
- Incremental synchronization keeps the semantic index aligned with changed music records.
- **Last.fm**, **YouTube Music**, **yt-dlp**, and **SpyderTRACK** contribute distinct functions to the music discovery and metadata workflow.
- Presentation, theme editing, music selection, playlist management, and playback are separated into dedicated modules.

---

SpyderMUSIC is free software licensed under **GNU AGPLv3**. See the repository's `README.md` and license documentation for setup, distribution, and third-party dependency information.
