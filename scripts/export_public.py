"""Create a verified source ZIP using the audited, explicit public allowlist."""
import argparse
import hashlib
import json
from pathlib import Path
from zipfile import ZIP_DEFLATED, ZipFile

try:
    from .audit_public import audit, scan_bytes
    from .public_files import read_public_files
except ImportError:
    from audit_public import audit, scan_bytes
    from public_files import read_public_files


def export_public(root, output):
    root, output = Path(root).resolve(), Path(output).resolve()
    result = audit(root)
    if not result["success"]:
        raise ValueError("Public source audit failed; export refused")
    if output.exists():
        raise FileExistsError("Refusing to overwrite an existing release")
    names = read_public_files(root)
    # Audit the frozen bytes that will actually enter the release.
    contents = {name: (root / name).read_bytes() for name in names}
    if any(scan_bytes(name, content) for name, content in contents.items()):
        raise ValueError("Frozen public source audit failed; export refused")
    output.parent.mkdir(parents=True, exist_ok=True)
    entries = {name: {"sha256": hashlib.sha256(data).hexdigest(), "size_bytes": len(data)}
               for name, data in contents.items()}
    manifest = {"schema_version": 1, "files": entries, "private_runtime_included": False}
    with output.open("xb") as stream:
        with ZipFile(stream, "w", ZIP_DEFLATED, compresslevel=9) as archive:
            for name, data in contents.items():
                archive.writestr("xhs-post-pipeline/" + name, data)
            archive.writestr("xhs-post-pipeline/RELEASE_MANIFEST.json", json.dumps(manifest, ensure_ascii=False, indent=2) + "\n")
    with ZipFile(output) as archive:
        if archive.testzip() is not None or len(archive.namelist()) != len(contents) + 1:
            raise ValueError("Release ZIP integrity failed")
        for name, data in contents.items():
            if archive.read("xhs-post-pipeline/" + name) != data:
                raise ValueError("Release ZIP source verification failed")
    return {"success": True, "archive": str(output), "source_file_count": len(contents),
            "sha256": hashlib.sha256(output.read_bytes()).hexdigest(), "crc_and_hashes_passed": True}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=Path(__file__).resolve().parents[1])
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args(argv)
    try:
        result = export_public(args.root, args.output)
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0
    except (OSError, ValueError) as error:
        print(json.dumps({"success": False, "error": str(error)}, ensure_ascii=False))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
