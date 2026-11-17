# SpyderMUSIC 🕷️🎵

**Free software for free music.**

SpyderMUSIC is an open-source music player and intelligent Auto DJ built with Python and Qt. It brings music discovery, personal taste learning, local playlists, and a highly customizable interface together in one application.

**Created with love for the SpyderMUSIC community. ❤️**

## Discover SpyderMUSIC.

SpyderMUSIC offers two independent ways to enjoy your music:

- **Auto DJ** discovers music and selects tracks with the help of Scout and Judge. Rather than repeating the same favorites, Judge balances musical fit, learned preferences, artist rotation, and controlled randomness.
- **Local Player** plays your own audio files with persistent playlists, a local queue, shuffle, and crossfades. No music discovery or downloads are required for local playback.

Both modes use the same **libmpv-based playback engine**. Switching modes changes future track selection without forcing the currently playing song to stop.

### Scout, Judge & AI Training

**Scout** discovers music and enriches the library with available metadata. **Judge** evaluates eligible tracks using semantic search, genre and vibe information, listening history, and learned preferences. The system is designed to discover music without becoming trapped in an endless loop of the highest-scoring songs.

**AI Training** gives Scout a head start: import a YouTube Music playlist, choose tracks, and assign a genre to build an initial preference base. A targeted discovery workflow can also start from **one song in the queue**.

Judge can consider separate global and vibe-specific feedback, cooldowns, and artist rotation when selecting music. Its decisions are documented in more detail in [JUDGE.md](JUDGE.md).

### Make the interface your own

SpyderMUSIC includes a visual **Theme Editor** and **QSS Studio**. Arrange and resize interface elements, manage layers, customize Qt styles, and use your own graphics for supported controls and visual elements. GPU-assisted rendering and audio-reactive visualizations allow themes to go beyond a static color scheme.

### Playlists & audio

The Local Player supports persistent playlists, drag-and-drop file and folder import, M3U/M3U8 import, M3U8 export, queue management, shuffle, track reordering, and missing-file detection. Its playlist controller uses the same two-deck playback engine and configurable crossfade system as Auto DJ.

For the complete feature inventory, see **[FEATURES.md](FEATURES.md)**.

> SpyderMUSIC is under active development. Features and optional integrations may require configuration or change between versions.

## Source distribution

This repository distributes SpyderMUSIC as **source code**, not as a prebuilt Windows executable or PyInstaller bundle.

The intended distribution includes the Python source, original themes and graphics, and the project's libmpv build with its accompanying license notices. Other Python dependencies are installed separately by the user; they are not redistributed as bundled binaries here.

## Requirements and setup

- **Python 3.13** (the version used to develop SpyderMUSIC).
- The Python packages required by the project, installed in your own environment.
- A compatible libmpv build and any runtime components required by that build.
- A **Last.fm API key** for Scout music discovery and metadata enrichment.
- A **contact email address** for Wikidata lookups of artist names written in non-Latin scripts, when using that functionality.

### API configuration

**Last.fm API key (required for Scout)**

Scout uses Last.fm to retrieve music metadata and tags. Obtain a free API key at
https://www.last.fm/api/account/create and enter it in SpyderMUSIC's settings.
If the key is missing, Scout reports that it is not configured rather than
silently appearing ready.

**Wikidata contact email (for non-Latin artist names)**

For Wikidata-assisted lookups of artist names written in scripts such as
Cyrillic, Japanese, or Korean, configure a valid contact email address in
SpyderMUSIC. This identifies the application's requests and provides a contact
point; a Wikidata account is not required for this purpose.

Neither configuration is needed simply to play local audio files.


Install the direct Python dependencies in a fresh Python 3.13 environment:

```powershell
py -3.13 -m venv .venv
.\.venv\Scripts\python.exe -m pip install --upgrade pip
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
.\.venv\Scripts\python.exe SpyderMUSIC.py
```

The commands assume that `requirements.txt` and `SpyderMUSIC.py` are present in the repository root. Consult the source and configuration for any additional optional services.

## Building Windows executables (optional)

The project provides one **PyInstaller spec configuration** for building your own Windows executable:

- **OneDir:** produces an application directory containing the executable and its bundled runtime files.

Install PyInstaller in the same environment:

```powershell
.\.venv\Scripts\python.exe -m pip install pyinstaller
.\.venv\Scripts\python.exe -m PyInstaller --clean --noconfirm SpyderMUSIC_OneDir.spec
```

**Required local build assets:** The documented specs expect `libmpv_windows-x86_64.dll` in the project root and `data\icon.ico` in the project data directory. Supply the matching libmpv build and project icon before building. The DLL's applicable third-party license notices must accompany any redistributed binary.

The spec configurations are designed to keep `yt-dlp`, `ytmusicapi`, and `yt-dlp-ejs` independently updatable at runtime. Preserve their collection logic and the required PyAV components when modifying the build. If included in your checkout, consult [BUILD_CHECKLIST.md](BUILD_CHECKLIST.md) for additional build checks.

These spec files are build configurations, **not precompiled releases**. Verify project-specific paths, dependencies, and assets against your checkout before building.

If you redistribute a build, **you are responsible for meeting the applicable license and notice requirements for every third-party component in that distribution**, as well as SpyderMUSIC's AGPLv3 requirements.

## libmpv

SpyderMUSIC uses a separately built libmpv library. Its build scripts and source references are published at:

**https://github.com/GozzaInc/libmpv-scripts**

The libmpv binary is **not** relicensed under SpyderMUSIC's AGPLv3. It retains its applicable upstream licenses. The license and third-party notices for the distributed build should accompany the binary; consult the matching build and revision for details.

## License

**SpyderMUSIC is licensed under the GNU Affero General Public License, version 3 (AGPLv3).** See [LICENSE.md](LICENSE.md) for the full license text.

**Scope:** This license covers the original SpyderMUSIC source code **and all original themes, QSS styles, artwork, icons, PNG/WebP graphics, and other visual assets created for SpyderMUSIC**, unless a file is explicitly marked otherwise.

You may use, study, modify, and redistribute these materials under the terms of the AGPLv3. When redistributing a modified version, comply with the applicable source-code and notice requirements.

Third-party software, including libmpv and independently installed Python dependencies, remains subject to its own licenses. Their inclusion or use does not change the licensing of those third-party components.

## Community

SpyderMUSIC was built to be shared, explored, redesigned, and improved. Fork the project, create your own themes, contribute fixes, and give the music player your own personality.

**Made with love for the SpyderMUSIC community. 🕷️❤️**
