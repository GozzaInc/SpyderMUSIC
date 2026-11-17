# -*- mode: python ; coding: utf-8 -*-
"""SpyderMUSIC M4: lean Windows build, CPU MiniLM and Qt Widgets/OpenGL.

The filters were checked against SpyderMUSIC M4 Python imports.
The dynamic MiniLM dependency tqdm is explicitly bundled.
A runtime hook for windowed multiprocessing is included in the spec.
"""

import ast
import sys
from importlib.util import find_spec
from importlib.metadata import distribution, PackageNotFoundError
from pathlib import Path
from PyInstaller.utils.hooks import collect_all

# Project files are relative to the spec in the SpyderMUSIC project directory.
datas = [
    ('data\\icon.ico', '.'),
    ('themes', 'themes'),
]
binaries = [('libmpv_windows-x86_64.dll', '.')]
hiddenimports = []


def add_package(name, *, include_module=None):
    """Collect binaries, runtime data and only required submodules.

    Do not copy duplicate Python sources as data into the runtime directory.
    PyInstaller already packs importable Python code into its PYZ archive.
    """
    package_data, package_binaries, modules = collect_all(
        name,
        include_py_files=False,
        filter_submodules=include_module or (lambda module: True),
    )
    datas.extend(package_data)
    binaries.extend(package_binaries)
    hiddenimports.extend(modules)


def keep_onnx_module(name):
    # Keep inference; exclude model training, conversion and quantization.
    return not name.startswith((
        'onnxruntime.backend',
        'onnxruntime.quantization',
        'onnxruntime.tools',
        'onnxruntime.transformers',
    ))


def keep_chroma_module(name):
    # Keep PersistentClient and ONNXMiniLM. Do not automatically add CLI, experiments or
    # HTTP server as hidden imports.
    # Analysis detects what PersistentClient actually imports.
    return not name.startswith((
        'chromadb.cli',
        'chromadb.experimental',
        'chromadb.server',
    ))


# --- Externally loadable music libraries (NOT in PYZ!) ---
# PyInstaller's FrozenImporter otherwise takes precedence over sys.path. This lets
# yt-dlp, ytmusicapi and yt-dlp-ejs remain replaceable in the built executable.
# The installed build versions provide the offline/fallback baseline.


def add_external_music_distribution(package, module):
    try:
        metadata = distribution(package)
    except PackageNotFoundError as exc:
        raise RuntimeError(
            f"{package} is missing from the build environment: "
            "python -m pip install -U 'yt-dlp[default]' ytmusicapi"
        ) from exc
    entries = metadata.files
    if entries is None:
        raise RuntimeError(f"{package}: No installed RECORD files found")
    number = 0
    for item in entries:
        relative = Path(str(item).replace('\\', '/'))
        if '__pycache__' in relative.parts or relative.suffix == '.pyc':
            continue
        top = relative.parts[0] if relative.parts else ''
        is_metadata = top.lower().endswith('.dist-info') and top.lower().replace('-', '_').startswith(
            package.replace('-', '_') + '_'
        )
        if top != module and not is_metadata:
            continue
        if '..' in relative.parts or relative.is_absolute():
            raise RuntimeError(f"{package}: Unsafe package path: {relative}")
        source = Path(metadata.locate_file(item))
        if source.is_file():
            datas.append((str(source), str(Path('music_packages') / relative.parent)))
            number += 1
    if not number:
        raise RuntimeError(f"{package}: No source files found for external bundling")


def collect_external_stdlib_imports(module_names, excluded_names):
    """Discover all stdlib imports of external packages so html.parser and similar modules
    end up in PYZ without bundling the external packages themselves.
    """
    stdlib_names = set(getattr(sys, 'stdlib_module_names', ()))
    excluded_roots = {item.split('.')[0] for item in excluded_names}
    discovered = set()

    for mod_name in module_names:
        spec = find_spec(mod_name)
        if spec is None or not spec.submodule_search_locations:
            continue
        for location in spec.submodule_search_locations:
            for py_file in Path(location).rglob('*.py'):
                try:
                    tree = ast.parse(py_file.read_text(encoding='utf-8', errors='ignore'))
                except SyntaxError:
                    continue
                for node in ast.walk(tree):
                    if isinstance(node, ast.Import):
                        for alias in node.names:
                            discovered.add(alias.name)
                    elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
                        discovered.add(node.module)
                        for alias in node.names:
                            if alias.name != '*':
                                discovered.add(f'{node.module}.{alias.name}')

    result = []
    for candidate in sorted(discovered):
        root = candidate.split('.')[0]
        if root not in stdlib_names or root in excluded_roots:
            continue
        try:
            if find_spec(candidate) is not None:
                result.append(candidate)
        except (ValueError, ModuleNotFoundError):
            continue
    return result


for _distribution, _module in (
    ('yt-dlp', 'yt_dlp'),
    ('ytmusicapi', 'ytmusicapi'),
    ('yt-dlp-ejs', 'yt_dlp_ejs'),
):
    add_external_music_distribution(_distribution, _module)

# Dependencies remain bundled in the runtime as before. The two
# main dynamic packages can be updated independently.
# packaging reads the version requirements for EJS declared on PyPI.
for _name in ('requests', 'urllib3', 'certifi', 'idna', 'charset_normalizer',
              'mutagen', 'websockets', 'Cryptodome', 'brotli', 'brotlicffi'):
    if find_spec(_name) is not None:
        add_package(_name)
add_package('packaging')

# --- Kernabhaengigkeiten ---
add_package('onnxruntime', include_module=keep_onnx_module)
add_package('chromadb', include_module=keep_chroma_module)
add_package('tokenizers', include_module=lambda name: not name.startswith('tokenizers.tools'))

# ChromaDB ONNXMiniLM_L6_V2 imports tqdm at runtime through importlib.
# Without explicit collection, the Python code is missing despite dist-info.
if find_spec('tqdm') is None:
    raise RuntimeError('tqdm is missing from the build environment. Run python -m pip install tqdm.')
add_package('tqdm')

# --- Unbenutzte Python-Pakete ---
excludes_list = [
    # No GUI alternative to Qt.
    'tkinter', '_tkinter', 'customtkinter', 'pywinstyles',
    # Unnoetige Geodaten-/Analyse-Stacks.
    'wradlib', 'xradar', 'xarray', 'cftime', 'netCDF4', 'pyproj',
    'matplotlib', 'contourpy', 'kiwisolver', 'h5py', 'dask', 'scipy',
    # M4 Chroma stores strings/metadata, not Arrow/Pandas tables.
    'pandas', 'pyarrow', 'pyarrow_hotfix',
    # Server/CLI/build overhead unused by local PersistentClient operation.
    'watchfiles', 'werkzeug', 'itsdangerous', 'click',
    'setuptools', 'pkg_resources', 'win32api', 'win32evtlog', 'win32pdh', 'pywintypes',
    # ONNX: inference only (MiniLM), no training/export tools.
    'onnxruntime.backend', 'onnxruntime.quantization',
    'onnxruntime.tools', 'onnxruntime.transformers',
    # Qt Widgets/OpenGL instead of QML, WebEngine or Virtual Keyboard.
    'PySide6.QtQml', 'PySide6.QtQmlModels', 'PySide6.QtQmlWorkerScript',
    'PySide6.QtQuick', 'PySide6.QtQuickWidgets',
    'PySide6.QtQuickControls2', 'PySide6.QtVirtualKeyboard',
    'PySide6.QtWebEngineCore', 'PySide6.QtWebEngineWidgets',
    'PySide6.QtWebEngineQuick', 'PySide6.QtWebChannel',
    'PySide6.QtHttpServer', 'PySide6.Qt3DCore', 'PySide6.Qt3DRender',
    'PySide6.QtDesigner', 'PySide6.QtPdf', 'PySide6.QtPdfWidgets',
    'PySide6.QtGraphs', 'PySide6.QtShaderTools',
    'PySide6.QtMultimedia', 'PySide6.QtMultimediaWidgets',
    'PySide6.QtSensors', 'PySide6.QtSerialPort',
    'PySide6.QtPositioning', 'PySide6.QtLocation',
    'PySide6.QtPrintSupport', 'PySide6.QtTest',
    'unittest', 'test',
    # These packages are stored as actual files in music_packages so updates
    # take effect without rebuilding. Do not include them in PyInstaller PYZ!
    'yt_dlp', 'ytmusicapi', 'yt_dlp_ejs',
]

# PyInstaller normally detects direct Qt imports; these
# explicit entries preserve the GPU compositor and SVG support.
hiddenimports.extend([
    'PySide6.QtCore', 'PySide6.QtGui', 'PySide6.QtWidgets',
    'PySide6.QtOpenGL', 'PySide6.QtOpenGLWidgets', 'PySide6.QtSvg',
    'av', 'av.audio', 'av.audio.resampler', 'PIL.Image',
    # Chroma also instantiates this embedding function dynamically.
    'chromadb.utils.embedding_functions.onnx_mini_lm_l6_v2',

    # --- Standard library for external yt-dlp and ytmusicapi ---
    'html',
    'html.parser',
    'html.entities',
    'http.cookiejar',
    'http.cookies',
    'xml.etree.ElementTree',
    'xml.sax',
    'xml.sax.saxutils',
    'email.utils',
    'email.header',
    'mimetypes',
    'fileinput',
    'netrc',
    'webbrowser',
    'uuid',
])
hiddenimports.extend(
    collect_external_stdlib_imports(('yt_dlp', 'ytmusicapi', 'yt_dlp_ejs'), excludes_list)
)
hiddenimports = sorted(set(hiddenimports))

a = Analysis(
    ['SpyderMUSIC.py'],
    pathex=[],
    binaries=binaries,
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    # With console=False, the frozen Chroma spawn process has no
    # stdout/stderr streams. The hook ensures that error messages
    # do not trigger a secondary failure in multiprocessing._bootstrap.
    runtime_hooks=[
        str(Path(SPECPATH) / 'spyder_stdio_hook.py'),
        str(Path(SPECPATH) / 'spyder_music_packages_hook.py'),
    ],
    excludes=excludes_list,
    noarchive=False,
    optimize=0,
)


def should_keep_file(destination):
    """Filter PyInstaller TOC destinations, never incidental source paths."""
    p = destination.replace('\\', '/').lower().lstrip('./')
    name = p.rsplit('/', 1)[-1]

    # Vollstaendig optionale, grosse Analysebibliotheken.
    if p.startswith(('pyarrow/', 'pyarrow.libs/', 'pandas/', 'pandas.libs/')):
        return False
    if p.startswith(('pyarrow-', 'pandas-')) and '.dist-info/' in p:
        return False

    # Remove type stubs, SBOMs, sample datasets and Emscripten workers.
    if p.endswith('.pyi') or '/sboms/' in p:
        return False
    if p.startswith(('onnxruntime/datasets/', 'chromadb/test/', 'urllib3/contrib/emscripten/')):
        return False

    # Pillow AVIF decoder (7.9 MB) is not required (PNG/WebP/JPEG remain).
    if p.startswith('pil/_avif'):
        return False

    # ONNX-Inferenz unter onnxruntime/capi bleibt unangetastet.
    if p.startswith((
        'onnxruntime/backend/', 'onnxruntime/quantization/',
        'onnxruntime/tools/', 'onnxruntime/transformers/',
    )):
        return False

    # Harmloser Paket-Ballast; SQL-Migrationen von Chroma bleiben drin.
    if p.startswith((
        'chromadb/experimental/', 'chromadb/cli/',
        'chromadb/utils/embedding_functions/schemas/benchmarks/',
        'jsonschema/benchmarks/',
    )):
        return False
    if '/tests/' in p or '/__tests__/' in p:
        return False

    if p.startswith('pyside6/'):
        if p.startswith(('pyside6/translations/', 'pyside6/qml/')):
            return False
        if p.startswith((
            'pyside6/plugins/platforminputcontexts/',
            'pyside6/plugins/tls/',
            'pyside6/plugins/generic/',
        )):
            return False
        if p.startswith((
            'pyside6/plugins/imageformats/qpdf',
            'pyside6/plugins/imageformats/qtiff',
            'pyside6/plugins/imageformats/qicns',
            'pyside6/plugins/imageformats/qtga',
            'pyside6/plugins/imageformats/qwbmp',
        )):
            return False
        # PyInstaller sometimes includes native Qt DLLs through plugin dependencies
        # even when their corresponding Python modules are excluded.
        # Core, Gui, Widgets, Network, OpenGL, Svg and image format plugins remain.
        if name.endswith(('.dll', '.pyd')) and name.startswith((
            'qt6qml', 'qt6quick', 'qt6pdf', 'qt6virtualkeyboard',
            'qtqml', 'qtquick', 'qtpdf', 'qtvirtualkeyboard',
        )):
            return False

    # Preserve the existing filters from the previous spec files.
    if any(piece in p for piece in (
        'opengl32sw', 'assistant.exe', 'designer.exe', 'qmlls.exe',
    )):
        return False
    return True


a.binaries = [entry for entry in a.binaries if should_keep_file(entry[0])]
a.datas = [entry for entry in a.datas if should_keep_file(entry[0])]

pyz = PYZ(a.pure)

# ONEDIR: SpyderMUSIC.exe plus runtime directory.
exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name='SpyderMUSIC',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    version='file_version_info.txt',
    icon=['data\\icon.ico'],
    contents_directory='runtime',
)

coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=True,
    upx_exclude=[],
    name='SpyderMUSIC',
)
