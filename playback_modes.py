"""Persisted playback policy. This module never owns or stops the audio engine.

Mode changes affect *future* selections. A currently playing track is untouched.
"""
from __future__ import annotations

from dataclasses import dataclass

AUTO_DJ = 'auto_dj'
OFFLINE = 'offline'
VALID_MODES = frozenset((AUTO_DJ, OFFLINE))


@dataclass(frozen=True)
class ModePolicy:
    mode: str
    discovery: bool
    enrichment: bool
    judge: bool
    local_only: bool


def policy_for(mode: str, *, offline_smart_selection: bool = False,
               offline_enrichment: bool = True) -> ModePolicy:
    if mode not in VALID_MODES:
        raise ValueError(f'Unknown playback mode: {mode!r}')
    if mode == AUTO_DJ:
        return ModePolicy(mode, True, True, True, False)
    return ModePolicy(mode, False, False, False, True)
