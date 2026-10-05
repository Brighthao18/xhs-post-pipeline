"""Audit only public files; never print matched secret values."""
import argparse
import json
from pathlib import Path
import re
import subprocess

try:
    from .public_files import read_public_files
except ImportError:
    from public_files import read_public_files

PATTERNS = {
    "private_user_path": re.compile(r"[A-Za-z]:[/\\]+Users[/\\]+(?!Public\b|Example\b)[^/\\\s\"']+", re.I),
    "private_workspace_path": re.compile(r"(?:[A-Za-z]:[/\\]+Coding[/\\]+|/home/[^/\s]+/\.codex/)", re.I),
    "private_key": re.compile(r"-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----"),
    "api_token": re.compile(r"\b(?:sk-[A-Za-z0-9_-]{24,}|gh[pousr]_[A-Za-z0-9]{30,}|AKIA[A-Z0-9]{16})\b"),
}
CREDENTIAL = re.compile(r'''["'](?:password|api_key|access_token|auth_token|secret_key)["']\s*:\s*["']([^"'\r\n]{8,})["']''', re.I)


def scan_bytes(name, data):
    findings = []
    try:
        content = data.decode("utf-8")
    except UnicodeError:
        return [{"path": name, "reason": "not_utf8"}]
    if content.startswith("\ufeff") or "\ufffd" in content:
        findings.append({"path": name, "reason": "encoding_artifact"})
    for reason, pattern in PATTERNS.items():
        if pattern.search(content):
            findings.append({"path": name, "reason": reason})
    is_fixture = "/tests/" in "/" + name or name.startswith("tests/") or name.endswith("_test.go")
    if not is_fixture:
        for matched in CREDENTIAL.finditer(content):
            value = matched.group(1)
            if not value.startswith(("offline-", "example-", "fixture-", "test-", "unit-", "fake-", "${", "<")):
                findings.append({"path": name, "reason": "credential_literal"})
                break
    return findings


def audit(root, check_git=False):
    root = Path(root).resolve()
    names = read_public_files(root)
    findings = [item for name in names for item in scan_bytes(name, (root / name).read_bytes())]
    if check_git:
        result = subprocess.run(["git", "-C", str(root), "ls-files", "-z"], capture_output=True, check=True)
        tracked = set(result.stdout.decode("utf-8").split("\0")) - {""}
        for name in sorted(tracked - set(names)):
            findings.append({"path": name, "reason": "tracked_outside_allowlist"})
        for name in sorted(set(names) - tracked):
            findings.append({"path": name, "reason": "public_file_not_staged"})
        for name in sorted(tracked & set(names)):
            staged = subprocess.run(["git", "-C", str(root), "cat-file", "blob", ":" + name], capture_output=True, check=True).stdout
            findings.extend({"path": entry["path"], "reason": "git_index_" + entry["reason"]} for entry in scan_bytes(name, staged))
    return {"success": not findings, "public_file_count": len(names), "findings": findings,
            "scope": "public source allowlist; heuristic checks do not replace human review"}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=Path(__file__).resolve().parents[1])
    parser.add_argument("--check-git", action="store_true")
    parser.add_argument("--report", type=Path)
    args = parser.parse_args(argv)
    try:
        result = audit(args.root, args.check_git)
    except (OSError, ValueError, subprocess.CalledProcessError):
        result = {"success": False, "error": "Public manifest or Git index validation failed"}
    text = json.dumps(result, ensure_ascii=False, indent=2) + "\n"
    if args.report:
        args.report.parent.mkdir(parents=True, exist_ok=True)
        args.report.write_text(text, encoding="utf-8")
    print(text, end="")
    return 0 if result["success"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
