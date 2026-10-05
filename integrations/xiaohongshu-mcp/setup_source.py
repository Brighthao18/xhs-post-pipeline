"""Fetch the pinned upstream into a new directory, then apply verified source overlays."""
import argparse
import hashlib
import json
from pathlib import Path, PurePosixPath
import subprocess


def setup_source(destination, upstream_source=None):
    package = Path(__file__).resolve().parent
    metadata = json.loads((package / "upstream.json").read_text(encoding="utf-8"))
    destination = Path(destination).resolve()
    if destination.exists():
        raise ValueError("Refusing to overwrite an existing backend checkout")
    source = str(Path(upstream_source).resolve()) if upstream_source else metadata["repository"]
    destination.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", "clone", "--no-hardlinks", "--no-checkout", source, str(destination)], check=True)
    subprocess.run(["git", "-C", str(destination), "checkout", "--detach", metadata["commit"]], check=True)
    for name, record in metadata["files"].items():
        relative = PurePosixPath(name)
        if relative.is_absolute() or ".." in relative.parts or ":" in name or "\\" in name:
            raise ValueError("Unsafe overlay path")
        source_path = (package / "overlay" / name).resolve(strict=True)
        if not source_path.is_relative_to((package / "overlay").resolve()):
            raise ValueError("Overlay source escaped its directory")
        data = source_path.read_bytes()
        if hashlib.sha256(data).hexdigest() != record["sha256"]:
            raise ValueError("Backend overlay hash mismatch")
        target = (destination / name).resolve()
        if not target.is_relative_to(destination):
            raise ValueError("Overlay target escaped its directory")
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(data)
    return {"source_path": str(destination), "upstream_commit": metadata["commit"], "overlay_files": len(metadata["files"])}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--destination", type=Path, required=True)
    parser.add_argument("--upstream-source", type=Path, help="Optional local upstream clone for offline preparation")
    args = parser.parse_args()
    print(json.dumps(setup_source(args.destination, args.upstream_source), ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
