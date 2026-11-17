# The SpyderMUSIC Judge 🕷️🎵

**Music selection that learns your taste without losing the freedom to explore.**

The **Judge** is SpyderMUSIC's candidate evaluation and selection system for **Auto DJ** mode. It is not a chatbot making a fresh guess for every song, nor is it a simple shuffle of liked tracks. It combines semantic retrieval, musical context, stored preferences, listening feedback, and controlled randomness.

This document describes the Judge behavior implemented in the SpyderMUSIC V4 engine. It distinguishes the **Scout**, which discovers and enriches music, from the **Judge**, which evaluates and selects from the available music database.

## 1. How the systems work together

1. **AI Training** provides an optional starting point: import a YouTube Music playlist, select songs, and assign a genre. Artist and track preferences are saved in SQLite and synchronized with ChromaDB.
2. **Scout** discovers or enriches music and supplies genre, tags, and other metadata. A targeted discovery workflow can also begin from a single song supplied through the queue.
3. **SpyderTRACK** can contribute researched song characteristics and a vibe profile where that information is available.
4. **ChromaDB** retrieves semantically relevant track candidates for the active genre and, when selected, the requested vibe.
5. **Judge** evaluates the candidates, applies exclusions and rotation rules, and chooses the next song.
6. **Playback feedback** updates the relevant preference scores, allowing later selections to reflect listening behavior.

The queue has priority over an autonomous Judge choice. A manually queued song is played because the user requested it; it is **not** counted as a Judge-selected rotation play.

## 2. Candidate retrieval and eligibility

The Judge queries ChromaDB using the configured genres and, when active, the resolved vibe description. It requests up to **90 results per query**, merges matching tracks, and combines semantic distances with the latest SQLite metadata.

Before ranking, it excludes tracks or artists that are:

- Explicitly blacklisted.
- Still within their configured track or artist cooldown.
- Otherwise unavailable from the current eligible candidate set.

Cooldowns prevent the system from repeating the same material too quickly. The configured genre and vibe describe the intended direction, not a permanent ban on musical variety.

## 3. Candidate scoring

The Judge uses different weightings depending on whether a vibe is active.

### With an active vibe

| Component | Weight |
| --- | ---: |
| Vibe fit | **60%** |
| Genre similarity | **25%** |
| Global personal taste | **15%** |

**Vibe fit** starts with **76% ChromaDB semantic similarity** and **24% tag/concept alignment**. If SpyderTRACK research is available, its track evidence and/or vibe profile may account for **up to 12% of the vibe-fit component**, replacing part of that component rather than adding an unlimited bonus.

The Judge then applies a **separate, bounded learned-vibe adjustment** of up to **±12 score points**. That adjustment grows in confidence with actual interactions: one observation matters less than four or more.

### Without an active vibe

| Component | Weight |
| --- | ---: |
| Genre similarity | **55%** |
| Global personal taste | **45%** |

There is no per-vibe adjustment when no vibe is selected.

### Why a high historical score does not automatically win

Global preference points are normalized through a bounded `tanh` curve. A track with hundreds of historical points can be favored, but it cannot simply overwhelm the requested musical context. The Judge is designed to respect taste **and** remain capable of discovering something new.

The ranking is an internal suitability score, not a promise that every selected song is an objective musical match.

## 4. Controlled randomness and temperature

The Judge sorts eligible candidates by score, then builds a **dynamic top-candidate pool**. Selection is weighted: stronger candidates are more likely, but the highest-scoring candidate is not guaranteed to win.

The configurable **temperature** controls how exploratory the choice is:

- **Lower temperature:** more concentrated on the strongest candidates.
- **Higher temperature:** a broader pool and softer preference differences.

In the inspected implementation, temperature is clamped to **0.1–1.0**. Pool size scales with temperature, with a minimum target of 12 candidates when enough eligible tracks exist. A weighted random draw chooses the initial winner.

This is deliberate. A DJ that always chooses the mathematically highest score eventually becomes a remarkably elaborate repeat button.

## 5. Artist rotation balancing

After the initial selection, the Judge can run a **rotation lottery** for the selected artist. Its purpose is to prevent one highly favored song from monopolizing that artist's plays.

Rotation counts are persisted in SQLite and incremented **only for tracks actually selected by the Judge**, not manually queued songs.

For the selected artist, the Judge compares the most-played and least-played tracks:

`discrepancy = (maximum plays - minimum plays) / maximum plays × 100`

- **Below 10% discrepancy:** retain the original Judge selection.
- **At least 10%:** consider a rotation lottery among eligible tracks by that artist.

The rotation filter still respects cooldowns, track blacklists, conflicting vibe concepts, and minimum suitability. It requires at least two eligible alternatives. It does **not** force an unsuitable song into the mix merely to equalize counts.

Lottery weight for each eligible track:

`tickets = maximum artist play count - track play count + 1`

For example, with artist play counts of **20, 15, and 5**, the respective lottery weights are **1, 6, and 16**. Underplayed tracks have better odds, not guaranteed selection.

## 6. Learning from playback

The engine distinguishes **global taste** from **context-specific vibe experience**. A song can work beautifully for one mood without needing to become a universal favorite.

### Default playback thresholds

| Event | Default behavior |
| --- | --- |
| Track passes **75%** playback | Eligible for a positive score, awarded once |
| Track skipped before **25%**, after more than 5 seconds | Eligible for an early-skip penalty |
| Normal successful play | **+10 points** |
| Normal early skip | **−15 points** |

These are the inspected V4 defaults; configuration and special playback paths can affect the outcome.

### When a vibe is active

- A Judge-selected successful play adds **+10** to that track's **vibe-specific score**, leaving its global score unchanged.
- A Judge-selected early skip applies **−15** to the vibe-specific score, leaving its global score unchanged.
- Vibe scores are stored per **track × vibe** on a **0–100** scale, together with an interaction count.
- An unseen track/vibe pairing starts from a neutral **50/100** when feedback is first recorded.

### When the user chooses a song

The system treats manual queue choices as stronger evidence of intent:

- **With a vibe:** a successful queued play gives **+15 vibe points**, plus **+5 global points** to the track and artist. An early skip gives **−10 vibe points**.
- **Without a vibe:** a successful queued play gives the normal **+10** plus a **+5 queue bonus** to the track and artist.

The engine also supports manual score adjustments. These use their own explicit update path instead of being treated as an ordinary automatic playback reward.

## 7. Scout, training, and the Judge are different jobs

**AI Training** is an intentional way to seed the music database with known selections and genre assignments. It helps the Scout start from a meaningful musical basis; it does not rewrite the Judge's scoring formula.

**Scout** is responsible for finding music and improving the available metadata. Its results make the Judge's later choices more informed.

**SpyderTRACK** supplies additional researched characteristics where available. Missing research does not automatically disqualify a track; the Judge can still use ChromaDB, genre, tags, and existing preferences.

**Judge** makes the autonomous selection from that evidence. It does not itself need to download music or run a separate playback engine.

A user can also put a particular song in the queue and use it as the reference for a targeted Scout search. This is a **discovery starting point**, not a claim that the Judge ignores every other signal during its subsequent ranking.

## 8. Transparency and fallback behavior

The AI log exposes the reasoning path, including:

- Active genre, resolved vibe, and temperature.
- Currently locked tracks and artists.
- Top-ranked candidates and their score breakdowns.
- Candidate-pool size and final selection.
- Rotation lottery participants, weights, and result when rotation is triggered.
- Fallback use or retrieval errors.

If semantic retrieval cannot produce a usable selection, the engine can fall back to its configured fallback track logic rather than treating the whole player as broken.

## 9. Design philosophy

The Judge balances three goals:

1. **Relevance:** choose music that fits the current direction.
2. **Learning:** react to what the user actually plays, skips, and explicitly queues.
3. **Discovery:** avoid collapsing into the same small group of proven favorites.

The result is a DJ that learns preferences without turning those preferences into a cage.

---

**Scope:** This document covers the Judge implementation and its direct interactions with Scout, AI Training, SpyderTRACK, ChromaDB, SQLite, and playback feedback. For the wider application, see [FEATURES.md](FEATURES.md).
