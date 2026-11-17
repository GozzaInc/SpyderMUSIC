"""Two independent playback policies; no playlist intelligence or theme selector."""
AUTO = 'auto_dj'
LOCAL = 'offline'
OPTIONS = (("Auto DJ", AUTO), ("Local Player", LOCAL))

def from_legacy(mode, smart=False):
    return LOCAL if mode in ('offline', 'hybrid') else AUTO

def legacy_mode(selection):
    if selection not in (AUTO, LOCAL):
        raise ValueError(selection)
    return selection
