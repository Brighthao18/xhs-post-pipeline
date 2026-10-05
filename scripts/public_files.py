"""The public source allowlist is shared by audit and export tools."""
from pathlib import Path, PurePosixPath

PRIVATE_PARTS = frozenset({".git", ".env", ".venv", ".claude", ".workbuddy", ".codebuddy",
                           "codex", "state", "session", "sessions", "__pycache__", "node_modules"})
PRIVATE_NAMES = frozenset({"cookies.json", "auth-token.json", "admin-credentials.json",
                           "xhs-automation.json", "queue.txt", "logo.jpg", "nul"})


def read_public_files(root):
    root = Path(root).resolve(strict=True)
    manifest = root / "public-files.txt"
    names, identities = [], set()
    for line in manifest.read_text(encoding="utf-8-sig").splitlines():
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        if line != line.strip() or "\\" in line or ":" in line:
            raise ValueError("Unsafe public manifest path")
        path = PurePosixPath(line)
        if path.is_absolute() or path.as_posix() != line or any(part in {".", ".."} for part in path.parts):
            raise ValueError("Unsafe public manifest path")
        if (set(part.lower() for part in path.parts) & PRIVATE_PARTS
                or path.parts[0].lower() in {"cookies", "output", "outputs", "work", "n8n_xhs_workflow"}
                or (path.parts[0].lower() == "config" and line != "config/xhs-automation.example.json")
                or (len(path.parts) == 2 and path.parts[0] == "pipeline" and path.name != "__init__.py")
                or path.name.lower() in PRIVATE_NAMES or path.name.lower().startswith(".env")
                or path.suffix.lower() in {".exe", ".dll", ".db", ".sqlite", ".zip", ".jpg", ".png"}):
            raise ValueError("Private or generated file in public manifest")
        if line.casefold() in identities:
            raise ValueError("Duplicate public manifest path")
        identities.add(line.casefold())
        candidate = root
        for part in path.parts:
            candidate = candidate / part
            if candidate.is_symlink() or getattr(candidate, "is_junction", lambda: False)():
                raise ValueError("Linked public files are not allowed")
        actual = candidate.resolve(strict=True)
        if not actual.is_relative_to(root) or not actual.is_file():
            raise ValueError("Public file escaped source root or is not a regular file")
        names.append(line)
    if not names or "public-files.txt" not in names:
        raise ValueError("Public manifest must include itself and at least one source file")
    return names
