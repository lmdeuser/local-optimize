#!/usr/bin/env python3
from __future__ import annotations

import argparse
import hashlib
import os
from pathlib import Path
import re
import shutil
import subprocess
import tarfile
import tempfile


def build_assets(output: Path) -> None:
    first_line = Path("debian/changelog").read_text().splitlines()[0]
    match = re.match(r"^(\S+) \((\S+)\)", first_line)
    if match is None:
        raise RuntimeError("Cannot parse debian/changelog")
    package, version = match.groups()
    upstream = version.rsplit("-", 1)[0]
    output.mkdir(parents=True, exist_ok=False)
    orig = output / f"{package}_{upstream}.orig.tar.gz"
    debian_tar = output / f"{package}_{version}.debian.tar.xz"

    with tempfile.TemporaryDirectory() as temporary:
        root = Path(temporary) / f"{package}-{upstream}"
        root.mkdir()
        paths = subprocess.check_output(["git", "ls-files", "-z"]).split(b"\0")
        for raw in paths:
            if not raw:
                continue
            relative = Path(os.fsdecode(raw))
            if relative.parts[0] in {
                ".github",
                ".claude",
                ".agents",
                "artifacts",
                "backup",
                "backups",
                "debian",
                "investigation",
                "models",
                "node_modules",
            }:
                continue
            source = Path(relative)
            destination = root / relative
            destination.parent.mkdir(parents=True, exist_ok=True)
            if source.is_symlink():
                destination.symlink_to(os.readlink(source))
            else:
                shutil.copy2(source, destination)
        with tarfile.open(orig, "w:gz") as archive:
            archive.add(root, arcname=root.name)

    with tarfile.open(debian_tar, "w:xz") as archive:
        archive.add("debian", arcname="debian")

    def digest(path: Path, algorithm: str) -> str:
        return hashlib.new(algorithm, path.read_bytes()).hexdigest()

    assets = {"orig": orig, "debian": debian_tar}
    section = ""
    result = []
    for line in Path("obs/base.dsc").read_text().splitlines():
        if line and not line[0].isspace():
            section = line.split(":", 1)[0]
        if line.startswith("Version:"):
            line = f"Version: {version}"
        elif line.startswith(" "):
            old_name = line.rsplit(" ", 1)[-1]
            kind = None
            if old_name.startswith(f"{package}_") and old_name.endswith(".orig.tar.gz"):
                kind = "orig"
            elif old_name.startswith(f"{package}_") and old_name.endswith(".debian.tar.xz"):
                kind = "debian"
            if kind is not None:
                asset = assets[kind]
                algorithm = {"Files": "md5", "Checksums-Sha1": "sha1", "Checksums-Sha256": "sha256"}[section]
                line = f" {digest(asset, algorithm)} {asset.stat().st_size} {asset.name}"
        result.append(line)
    dsc = output / f"{package}_{version}.dsc"
    dsc.write_text("\n".join(result) + "\n")
    print(f"Built source assets for {package} {version}")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output-dir", type=Path, required=True)
    args = parser.parse_args()
    build_assets(args.output_dir)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
