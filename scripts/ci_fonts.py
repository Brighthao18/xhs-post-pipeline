"""Fetch a pinned, open-licensed CJK font into a CI-only temporary directory."""
import argparse
import hashlib
import os
from pathlib import Path
from urllib.request import urlopen

BASE = "https://raw.githubusercontent.com/notofonts/noto-cjk/f8d157532fbfaeda587e826d4cd5b21a49186f7c/Serif/"
FILES = {
    "cjk.otf": ("OTF/SimplifiedChinese/NotoSerifCJKsc-Regular.otf", "2a2eae2628df83556c54018c41e20fa532c1b862c5256ae8b3f23feb918d12ca"),
    "LICENSE.txt": ("LICENSE", "6a73f9541c2de74158c0e7cf6b0a58ef774f5a780bf191f2d7ec9cc53efe2bf2"),
}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--directory", type=Path, required=True)
    args = parser.parse_args()
    directory = args.directory.resolve()
    directory.mkdir(parents=True, exist_ok=True)
    for name, (relative, expected) in FILES.items():
        path = directory / name
        data = path.read_bytes() if path.is_file() else None
        if data is None or hashlib.sha256(data).hexdigest() != expected:
            with urlopen(BASE + relative, timeout=30) as response:
                data = response.read(30 * 1024 * 1024)
            if hashlib.sha256(data).hexdigest() != expected:
                raise ValueError("CI font download did not match its pinned hash")
            path.write_bytes(data)
    if os.environ.get("GITHUB_ENV"):
        with Path(os.environ["GITHUB_ENV"]).open("a", encoding="utf-8") as stream:
            stream.write("XHS_CJK_FONT=" + str(directory / "cjk.otf") + "\n")
    print("Pinned open-licensed test font verified")


if __name__ == "__main__":
    main()
