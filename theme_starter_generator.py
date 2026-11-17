#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""SpyderMUSIC: Generates theme_starter_data.py from an exported theme.

Usage: python theme_starter_generator.py "Mad_Scientist_EMPTY.zip"
Or:   python theme_starter_generator.py "path/to/theme-folder"
Without arguments, a file/folder dialog opens (Tkinter).

The result embeds QSS, JSON and every asset from assets/ directly in Python code.
The existing theme generator continues to use starter_files().
"""
from __future__ import annotations

import argparse
import base64
import json
import pathlib
import sys
import textwrap
import zipfile
import zlib

DEFAULT_OUTPUT = "theme_starter_data.py"


def choose_source() -> pathlib.Path:
    try:
        import tkinter as tk
        from tkinter import filedialog, messagebox
        root = tk.Tk()
        root.withdraw()
        path = filedialog.askopenfilename(
            title="SpyderMUSIC: Select theme ZIP",
            filetypes=[("Theme ZIP", "*.zip"), ("All files", "*.*")],
        )
        if not path and messagebox.askyesno("Theme source", "Select a theme folder instead?"):
            path = filedialog.askdirectory(title="Select theme folder")
        root.destroy()
        if not path:
            raise SystemExit("Cancelled: No theme source selected.")
        return pathlib.Path(path)
    except ImportError:
        raise SystemExit("Tkinter is unavailable. Pass a ZIP or folder as an argument.")


def load_source(source: pathlib.Path) -> dict[str, bytes]:
    if source.is_file() and source.suffix.lower() == ".zip":
        with zipfile.ZipFile(source) as archive:
            bad = archive.testzip()
            if bad:
                raise ValueError(f"Corrupt ZIP: {bad}")
            raw = {info.filename.replace('\\', '/'): archive.read(info)
                   for info in archive.infolist() if not info.is_dir()}
    elif source.is_dir():
        raw = {path.relative_to(source).as_posix(): path.read_bytes()
               for path in source.rglob('*') if path.is_file()}
    else:
        raise ValueError(f"Not a valid theme folder or ZIP: {source}")

    json_files = [n for n in raw if n.lower().endswith('.json') and '/' not in n]
    qss_files = [n for n in raw if n.lower().endswith('.qss') and '/' not in n]
    if len(json_files) != 1 or len(qss_files) != 1:
        raise ValueError("The theme root must contain exactly one JSON file and one QSS file.")
    json_name, qss_name = json_files[0], qss_files[0]
    if pathlib.PurePosixPath(json_name).stem != pathlib.PurePosixPath(qss_name).stem:
        raise ValueError("JSON and QSS must share the same base filename.")
    try:
        theme_json = json.loads(raw[json_name].decode('utf-8-sig'))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError(f"Invalid theme JSON: {exc}") from exc
    if not isinstance(theme_json, dict):
        raise ValueError("Theme JSON must contain an object.")
    raw[qss_name].decode('utf-8-sig')  # validate text early
    result = {n: data for n, data in raw.items()
              if n in (json_name, qss_name) or n.startswith('assets/')}
    if not result:
        raise ValueError("No theme files found.")
    return result


def build_module(files: dict[str, bytes]) -> str:
    lines = [
        '"""Embedded starter theme. No external template file required.',
        'Generated locally by theme_starter_generator.py.',
        '"""', '', 'import base64', 'import zlib', '', '_EMBEDDED = {',
    ]
    for name, content in sorted(files.items()):
        encoded = base64.b64encode(zlib.compress(content, level=9)).decode('ascii')
        lines.append(f'    {name!r}: (')
        for chunk in textwrap.wrap(encoded, width=112):
            lines.append(f'        {chunk!r}')
        lines.append('    ),')
    lines += [
        '}', '',
        'def starter_files():',
        '    """Return new byte buffers for all starter files, including embedded PNG."""',
        "    return {name: zlib.decompress(base64.b64decode(''.join(parts)))",
        '            for name, parts in _EMBEDDED.items()}', '',
    ]
    return '\n'.join(lines)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('source', nargs='?', help='Theme ZIP or theme folder')
    parser.add_argument('-o', '--output', help='Output Python file (default: next to this script)')
    parser.add_argument('--force', action='store_true', help='Overwrite existing output file (creates a backup)')
    args = parser.parse_args()
    source = pathlib.Path(args.source) if args.source else choose_source()
    output = pathlib.Path(args.output) if args.output else pathlib.Path(__file__).resolve().parent / DEFAULT_OUTPUT
    try:
        files = load_source(source)
        generated = build_module(files)
        # Before writing, verify syntax and byte-for-byte restoration in memory.
        namespace: dict = {}
        compile(generated, str(output), 'exec')
        exec(compile(generated, str(output), 'exec'), namespace)
        restored = namespace['starter_files']()
        if restored != files:
            raise ValueError('Internal byte comparison failed; nothing was written.')
        if output.exists():
            if not args.force:
                print(f'Output file already exists: {output}')
                answer = input('Overwrite and create a .bak backup first? [y/N] ').strip().lower()
                if answer not in ('y', 'yes'):
                    print('Cancelled. Existing file left unchanged.')
                    return 1
            backup = output.with_name(output.name + '.bak')
            backup.write_bytes(output.read_bytes())
            print(f'Backup: {backup}')
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(generated, encoding='utf-8', newline='\n')
        print(f'SUCCESS: {output}')
        for name, data in sorted(files.items()):
            print(f'  {name} ({len(data):,} Bytes)')
        print('All files were verified byte for byte.')
        return 0
    except (ValueError, OSError, zipfile.BadZipFile) as exc:
        print(f'ERROR: {exc}', file=sys.stderr)
        return 2


if __name__ == '__main__':
    sys.exit(main())
