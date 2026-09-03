#!/usr/bin/env python3
"""Build the release archive, deterministically, and prove what is in it.

Two properties matter for a release artefact that people install by hand.

It has to be reproducible: the same commit must produce the same bytes, so
anyone can rebuild the zip from the tag and compare the checksum against the
one published with the release. Python's ``zipfile`` does not give that for
free — it stamps each entry with the file's mtime, which is the checkout time,
so two builds of one commit differ. Everything below fixes the parts that
would otherwise carry the build machine into the artefact: entry order, the
timestamp, the permission bits and the compression level.

And it has to contain what the tag contains, nothing else. The file list comes
from ``git ls-files`` rather than a directory walk, so a stray ``.env``, an
editor backup or a ``__pycache__`` left in the working tree cannot be shipped.
The archive is then reopened and checked against that same list, because the
interesting failure is not the build raising, it is the build quietly writing
an archive that is missing a platform module.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import pathlib
import subprocess
import sys
import zipfile

REPO = pathlib.Path(__file__).resolve().parent.parent
PACKAGE = pathlib.Path("custom_components/unifi_play")

#: The DOS epoch, which is the earliest a zip entry can record. Any fixed
#: value works; this one is recognisably "not a real time" in a listing.
FIXED_TIMESTAMP = (1980, 1, 1, 0, 0, 0)

#: 0644, as a zip external attribute. Whatever umask the builder had, and
#: whether or not the checkout preserved a mode bit, does not reach the file
#: the user unpacks.
FIXED_MODE = 0o644 << 16


def tracked_files() -> list[pathlib.Path]:
    """Every file git has under the integration package, sorted."""
    out = subprocess.run(
        ["git", "ls-files", "-z", str(PACKAGE)],
        cwd=REPO,
        check=True,
        capture_output=True,
        text=True,
    ).stdout
    files = [pathlib.Path(name) for name in out.split("\0") if name]
    if not files:
        raise SystemExit(f"git ls-files found nothing under {PACKAGE}")
    return sorted(files)


def arcname(path: pathlib.Path) -> str:
    """Path inside the archive: ``unifi_play/...``.

    The archive unpacks into ``config/custom_components/``, so the directory
    it creates has to be the one Home Assistant loads.
    """
    return str(pathlib.Path("unifi_play") / path.relative_to(PACKAGE))


def build(target: pathlib.Path, files: list[pathlib.Path]) -> None:
    target.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(
        target, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=9
    ) as archive:
        for path in files:
            info = zipfile.ZipInfo(arcname(path), date_time=FIXED_TIMESTAMP)
            info.compress_type = zipfile.ZIP_DEFLATED
            info.external_attr = FIXED_MODE
            # 3 is Unix. The default is taken from the build platform, which
            # would put the builder's OS in the archive header.
            info.create_system = 3
            archive.writestr(info, (REPO / path).read_bytes())


def verify(target: pathlib.Path, files: list[pathlib.Path], version: str | None) -> str:
    """Reopen the archive and check it against the tracked file list."""
    with zipfile.ZipFile(target) as archive:
        broken = archive.testzip()
        if broken is not None:
            raise SystemExit(f"corrupt entry in archive: {broken}")
        names = sorted(archive.namelist())
        expected = sorted(arcname(path) for path in files)
        if names != expected:
            missing = set(expected) - set(names)
            extra = set(names) - set(expected)
            raise SystemExit(
                f"archive contents do not match the commit: "
                f"missing={sorted(missing)} unexpected={sorted(extra)}"
            )
        manifest = json.loads(archive.read("unifi_play/manifest.json"))

    if version is not None and manifest["version"] != version:
        raise SystemExit(
            f"manifest.json says {manifest['version']}, release is {version}. "
            "Bump the manifest in the same commit as the tag."
        )
    return hashlib.sha256(target.read_bytes()).hexdigest()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", default="dist/unifi_play.zip")
    parser.add_argument(
        "--version",
        help="Release version without the leading v. Checked against manifest.json.",
    )
    args = parser.parse_args()

    target = pathlib.Path(args.output)
    if not target.is_absolute():
        target = REPO / target

    files = tracked_files()
    build(target, files)
    digest = verify(target, files, args.version)

    print(f"{len(files)} files -> {target}")
    print(f"sha256  {digest}")
    checksum = target.with_name(target.name + ".sha256")
    checksum.write_text(f"{digest}  {target.name}\n", encoding="utf-8")
    return 0


if __name__ == "__main__":
    sys.exit(main())
