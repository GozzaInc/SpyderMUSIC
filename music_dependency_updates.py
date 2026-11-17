"""Independent yt-dlp and ytmusicapi updates, including frozen SpyderMUSIC.

Normal Python: the established pip upgrade behavior is unchanged.
Frozen EXE: download official *pure-Python wheels* from PyPI over HTTPS,
validate PyPI's SHA-256 digest, stage versioned files and atomically activate
them for the NEXT application launch. No pip, installed Python, EXE replacement
or live module reload is needed. yt-dlp-ejs is updated as a matching pair.
"""

import argparse
import hashlib
import importlib.metadata
import io
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import urllib.parse
import urllib.request
import zipfile
from email.parser import BytesParser
from pathlib import Path, PurePosixPath

from music_package_bootstrap import (
    PACKAGE_MODULES, installed_release_dir, persistent_package_dir, read_active,
)

PACKAGE_NAMES = tuple(PACKAGE_MODULES)
UPDATE_TIMEOUT_SECONDS = 45
MAX_WHEEL_BYTES = 40 * 1024 * 1024
MAX_UNPACKED_BYTES = 100 * 1024 * 1024
MAX_WHEEL_FILES = 8_000
_PYPI_ROOT = "https://pypi.org/pypi"


class MusicPackageUpdateError(RuntimeError):
    """Recoverable update error; existing packages remain usable."""


def _version(package):
    try:
        return importlib.metadata.version(package)
    except importlib.metadata.PackageNotFoundError:
        return None


def _safe_url(url, *, wheel=False):
    parsed = urllib.parse.urlparse(url)
    if parsed.scheme != "https" or parsed.hostname not in ("pypi.org", "files.pythonhosted.org"):
        raise MusicPackageUpdateError("Untrusted package download URL")
    if wheel and not parsed.path.endswith(".whl"):
        raise MusicPackageUpdateError("Expected a Python wheel")
    return url


def _https_bytes(url, limit):
    _safe_url(url)
    request = urllib.request.Request(url, headers={"User-Agent": "SpyderMUSIC-M4-MusicUpdater/1.0"})
    try:
        with urllib.request.urlopen(request, timeout=UPDATE_TIMEOUT_SECONDS) as response:
            _safe_url(response.geturl())
            content_length = response.headers.get("Content-Length")
            if content_length and int(content_length) > limit:
                raise MusicPackageUpdateError("Package exceeds download limit")
            data = response.read(limit + 1)
            if len(data) > limit:
                raise MusicPackageUpdateError("Package exceeds download limit")
            return data
    except (OSError, ValueError, TimeoutError) as exc:
        raise MusicPackageUpdateError(f"Network error: {exc}") from exc


def _pypi_json(distribution, version=None):
    if distribution not in ("yt-dlp", "ytmusicapi", "yt-dlp-ejs"):
        raise ValueError(f"Unsupported package: {distribution}")
    suffix = f"/{version}" if version else ""
    raw = _https_bytes(f"{_PYPI_ROOT}/{distribution}{suffix}/json", 5 * 1024 * 1024)
    try:
        result = json.loads(raw)
        if not isinstance(result, dict) or not isinstance(result.get("info"), dict):
            raise ValueError("Missing package information")
        return result
    except (ValueError, UnicodeDecodeError) as exc:
        raise MusicPackageUpdateError(f"Invalid PyPI response: {exc}") from exc


def _wheel_from_release(payload):
    """Require a platform-independent wheel (no C ABI or DLL updates)."""
    from packaging.specifiers import SpecifierSet

    info = payload["info"]
    required_python = info.get("requires_python") or ""
    if required_python and f"{sys.version_info.major}.{sys.version_info.minor}.{sys.version_info.micro}" not in SpecifierSet(required_python):
        raise MusicPackageUpdateError(f"Release requires Python {required_python}")
    wheel_list = payload.get("urls", ())
    candidates = [item for item in wheel_list
                  if item.get("packagetype") == "bdist_wheel"
                  and not item.get("yanked")
                  and re.search(r"-py(?:2\.py3|3)-none-any\.whl\Z", item.get("filename", ""))]
    if not candidates:
        raise MusicPackageUpdateError("No compatible pure-Python wheel on PyPI")
    wheel = candidates[0]
    if not isinstance(wheel.get("digests", {}).get("sha256"), str):
        raise MusicPackageUpdateError("PyPI SHA-256 is missing")
    _safe_url(wheel["url"], wheel=True)
    return wheel


def _wheel_bytes(wheel):
    payload = _https_bytes(wheel["url"], MAX_WHEEL_BYTES)
    if hashlib.sha256(payload).hexdigest().lower() != wheel["digests"]["sha256"].lower():
        raise MusicPackageUpdateError("Wheel SHA-256 mismatch; update rejected")
    return payload


def _unpack_pure_wheel(raw, distribution, version, dest):
    """Extract allowlisted package and metadata paths, reject zip-slip/symlinks."""
    main_module = {"yt-dlp": "yt_dlp", "ytmusicapi": "ytmusicapi", "yt-dlp-ejs": "yt_dlp_ejs"}[distribution]
    normalized = distribution.replace("-", "_")
    found_metadata = False
    try:
        with zipfile.ZipFile(io.BytesIO(raw)) as archive:
            members = archive.infolist()
            if len(members) > MAX_WHEEL_FILES:
                raise MusicPackageUpdateError("Too many files in wheel")
            total = 0
            for member in members:
                filename = member.filename
                rel = PurePosixPath(filename)
                if (rel.is_absolute() or ".." in rel.parts or "\\" in filename
                        or not rel.parts or "\x00" in filename):
                    raise MusicPackageUpdateError("Unsafe wheel path")
                if member.is_dir():
                    continue
                total += member.file_size
                if total > MAX_UNPACKED_BYTES:
                    raise MusicPackageUpdateError("Wheel exceeds unpack limit")
                # Prevent links and native executable code. These three wheels
                # must stay pure Python; other dependencies remain in runtime.
                if (member.external_attr >> 16) & 0o170000 == 0o120000:
                    raise MusicPackageUpdateError("Wheel contains a symlink")
                if rel.suffix.lower() in (".pyd", ".dll", ".so", ".dylib", ".exe", ".bat", ".cmd"):
                    raise MusicPackageUpdateError("Native executable in pure-Python wheel")
                top = rel.parts[0]
                allowed_metadata = top.endswith(".dist-info") and top.lower().replace("-", "_").startswith(normalized + "_")
                if top != main_module and not allowed_metadata:
                    # Skip wheel installer-generated paths, console scripts and
                    # other unrelated top-level files.
                    continue
                if allowed_metadata and rel.name == "METADATA":
                    meta = BytesParser().parsebytes(archive.read(member))
                    if (meta.get("Name", "").lower().replace("_", "-") != distribution
                            or meta.get("Version") != version):
                        raise MusicPackageUpdateError("Wheel metadata does not match PyPI release")
                    found_metadata = True
                dest_file = dest.joinpath(*rel.parts)
                dest_file.parent.mkdir(parents=True, exist_ok=True)
                with archive.open(member) as source, dest_file.open("wb") as target:
                    shutil.copyfileobj(source, target)
            if not found_metadata or not (dest / main_module).is_dir():
                raise MusicPackageUpdateError("Wheel package or metadata missing")
            if distribution != "yt-dlp-ejs" and not (dest / main_module / "__init__.py").is_file():
                raise MusicPackageUpdateError("Python package __init__.py missing")
    except (zipfile.BadZipFile, EOFError, OSError) as exc:
        raise MusicPackageUpdateError(f"Corrupted wheel: {exc}") from exc



def _ensure_existing_dependencies(metadata, own_package):
    """Do not activate a future wheel requiring an unbundled core dependency.

    Optional extras can remain optional; EJS is handled as an atomic yt-dlp
    companion. This check stops a future incompatible wheel from making the
    packaged app unstartable simply because PyPI added a new dependency.
    """
    from packaging.requirements import Requirement
    for raw in metadata.get("requires_dist") or ():
        try:
            req = Requirement(raw)
            if req.marker and not req.marker.evaluate({"extra": ""}):
                continue
            dep = req.name.lower().replace("_", "-")
            if dep in (own_package, "yt-dlp-ejs"):
                continue
            present = _version(dep)
            if present is None or (req.specifier and not req.specifier.contains(present)):
                raise MusicPackageUpdateError(
                    f"New version needs {req}; bundled dependency "
                    f"{dep}={present or 'missing'}; rebuild required"
                )
        except MusicPackageUpdateError:
            raise
        except Exception as exc:
            raise MusicPackageUpdateError(f"Invalid dependency declaration: {exc}") from exc


def _ejs_requirement(yt_dlp_metadata):
    """Use the exact version constraint yt-dlp declares for its default extra."""
    from packaging.requirements import Requirement
    for raw in yt_dlp_metadata.get("requires_dist") or ():
        try:
            requirement = Requirement(raw)
            if requirement.name.lower().replace("_", "-") != "yt-dlp-ejs":
                continue
            if requirement.marker and not requirement.marker.evaluate({"extra": "default"}):
                continue
            return requirement.specifier
        except Exception as exc:
            raise MusicPackageUpdateError(f"Invalid yt-dlp EJS requirement: {exc}") from exc
    raise MusicPackageUpdateError("yt-dlp does not declare a default EJS dependency")


def _ejs_release(requirement):
    from packaging.version import Version, InvalidVersion
    releases = _pypi_json("yt-dlp-ejs").get("releases", {})
    valid = []
    for ver, entries in releases.items():
        try:
            if not requirement.contains(Version(ver), prereleases=False):
                continue
            if not any(item.get("packagetype") == "bdist_wheel" and not item.get("yanked") for item in entries):
                continue
            valid.append(Version(ver))
        except (InvalidVersion, ValueError):
            continue
    if not valid:
        raise MusicPackageUpdateError(f"No yt-dlp-ejs wheel matches {requirement}")
    return _pypi_json("yt-dlp-ejs", str(max(valid)))


def _activate(package, version, folder):
    """Atomic pointer update: partial downloads never become active."""
    target = persistent_package_dir()
    target.mkdir(parents=True, exist_ok=True)
    current = read_active()
    current[package] = {"version": version, "folder": folder}
    content = json.dumps({"schema": 1, "active": current}, ensure_ascii=False, indent=2)
    fd, temporary = tempfile.mkstemp(prefix="active-", suffix=".json.tmp", dir=target)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, target / "active.json")
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def _update_frozen_one(package, report):
    """Stage a complete version; make it active only after all checks pass."""
    metadata = _pypi_json(package)
    version = metadata["info"].get("version")
    if not isinstance(version, str) or not re.fullmatch(r"[a-zA-Z0-9][a-zA-Z0-9._+-]{0,80}", version):
        raise MusicPackageUpdateError("Invalid PyPI release version")
    _ensure_existing_dependencies(metadata["info"], package)
    ejs_metadata = None
    if package == "yt-dlp":
        ejs_metadata = _ejs_release(_ejs_requirement(metadata["info"]))

    ejs_version = ejs_metadata["info"]["version"] if ejs_metadata else None
    old_version = _version(package)
    old_ejs = _version("yt-dlp-ejs") if package == "yt-dlp" else None
    if old_version == version and (ejs_version is None or ejs_version == old_ejs):
        report(f"{package}: {version} up to date" + (f" (EJS {old_ejs})" if old_ejs else ""))
        return

    main_wheel = _wheel_from_release(metadata)
    ejs_wheel = _wheel_from_release(ejs_metadata) if ejs_metadata else None
    folder = version + "-" + main_wheel["digests"]["sha256"][:12]
    location = persistent_package_dir() / "releases" / package
    location.mkdir(parents=True, exist_ok=True)
    release = location / folder
    if not (release / PACKAGE_MODULES[package]).is_dir() or (ejs_wheel and not (release / "yt_dlp_ejs").is_dir()):
        staging = Path(tempfile.mkdtemp(prefix=".staging-", dir=location))
        try:
            _unpack_pure_wheel(_wheel_bytes(main_wheel), package, version, staging)
            if ejs_wheel:
                _unpack_pure_wheel(_wheel_bytes(ejs_wheel), "yt-dlp-ejs", ejs_version, staging)
            # Do not modify a loaded version. Previously activated versioned
            # directories stay untouched for rollback.
            if not release.exists():
                os.replace(staging, release)
            else:
                # An existing version should never be overwritten while the
                # app may be importing it. Reuse it only if complete.
                if not (release / PACKAGE_MODULES[package] / "__init__.py").is_file():
                    raise MusicPackageUpdateError("Existing release directory is incomplete")
                if ejs_wheel and not (release / "yt_dlp_ejs").is_dir():
                    raise MusicPackageUpdateError("Existing EJS release is incomplete")
        finally:
            if staging.exists():
                shutil.rmtree(staging, ignore_errors=True)

    entry = {"version": version, "folder": folder}
    if installed_release_dir(package, entry) is None:
        raise MusicPackageUpdateError("Installed wheel failed release validation")
    _activate(package, version, folder)
    report(f"{package}: {old_version or 'not installed'} → {version}" +
           (f" (EJS {old_ejs or '?'} → {ejs_version})" if ejs_version else "") +
           " | active after restart")


def _update_source_one(package, report):
    """Normal source run: retain the original pip workflow."""
    previous = _version(package)
    arguments = [sys.executable, "-m", "pip", "install", "--upgrade",
                 "--disable-pip-version-check", "--no-input",
                 "yt-dlp[default]" if package == "yt-dlp" else package]
    try:
        completed = subprocess.run(
            arguments, capture_output=True, text=True, timeout=180,
            creationflags=(getattr(subprocess, "CREATE_NO_WINDOW", 0) if os.name == "nt" else 0),
        )
        if completed.returncode:
            raise MusicPackageUpdateError((completed.stderr or completed.stdout or "pip failed").strip()[-350:])
        now = _version(package)
        if now and now != previous:
            report(f"{package}: {previous or 'not installed'} → {now} | active after restart")
        else:
            report(f"{package}: {now or 'Installation complete'} up to date")
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise MusicPackageUpdateError(str(exc)) from exc


def update_music_libraries(packages=PACKAGE_NAMES, report=print):
    """Update packages separately in the existing GUI worker thread."""
    success = True
    for package in packages:
        if package not in PACKAGE_NAMES:
            raise ValueError(f"Unrecognized music package: {package}")
        try:
            if getattr(sys, "frozen", False):
                _update_frozen_one(package, report)
            else:
                _update_source_one(package, report)
        except (MusicPackageUpdateError, OSError, ValueError, KeyError, TypeError) as exc:
            success = False
            report(f"{package}: Update failed ({str(exc)[-350:]}); previous version remains active")
    return success


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Update SpyderMUSIC music packages")
    select = parser.add_mutually_exclusive_group()
    select.add_argument("--yt-dlp", action="store_true")
    select.add_argument("--ytmusicapi", action="store_true")
    args = parser.parse_args()
    chosen = ("yt-dlp",) if args.yt_dlp else (("ytmusicapi",) if args.ytmusicapi else PACKAGE_NAMES)
    raise SystemExit(0 if update_music_libraries(chosen) else 1)
