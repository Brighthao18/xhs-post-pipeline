"""Portable defaults and public export boundaries; never touch real accounts."""
import contextlib
import io
import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch
from zipfile import ZipFile

from pipeline.v2.__main__ import main
from pipeline.v2.config import BUNDLED_SKILL_DIR, initialize_config, load_config
from pipeline.v2.quality import generator_module, normalize_draft
from scripts.audit_public import audit, scan_bytes
from scripts.export_public import export_public
from scripts.public_files import read_public_files


class PortableConfigTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="xhs-public-config-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.profile = self.root / "config/local.json"

    def test_init_is_private_disabled_and_preserves_existing_files(self):
        initialize_config(self.root, self.profile, author="本地作者")
        original = self.profile.read_bytes()
        data = load_config(self.profile)
        self.assertFalse(data["policy"]["allow_publish"])
        self.assertFalse(data["policy"]["submit_backend_ready"])
        self.assertEqual(data["sources"], [])
        self.assertEqual(data["account"]["account_id"], "")
        self.assertFalse((self.root / "Codex").exists())
        self.assertEqual(Path(data["skill_dir"]), BUNDLED_SKILL_DIR)
        with self.assertRaises(FileExistsError):
            initialize_config(self.root, self.profile, author="其他作者")
        self.assertEqual(self.profile.read_bytes(), original)

    def test_relative_paths_do_not_depend_on_current_directory(self):
        self.profile.parent.mkdir()
        data = {"schema_version": 2, "workspace": "..", "skill_dir": "custom-generator",
                "sources": [{"id": "fixture", "path": "feed.json", "html_path": "article.html",
                             "refresh": {"credentials_path": "Codex/state/credentials.json"}}],
                "publication_backend": {"auth_token_path": "Codex/state/backend/auth-token.json"}}
        self.profile.write_text(json.dumps(data), encoding="utf-8")
        previous = Path.cwd()
        with tempfile.TemporaryDirectory() as elsewhere:
            try:
                os.chdir(elsewhere)
                loaded = load_config(self.profile)
            finally:
                os.chdir(previous)
        self.assertEqual(loaded["workspace"], str(self.root.resolve()))
        self.assertEqual(loaded["skill_dir"], str((self.root / "custom-generator").resolve()))
        self.assertEqual(loaded["sources"][0]["path"], str((self.root / "feed.json").resolve()))
        self.assertEqual(loaded["sources"][0]["refresh"]["credentials_path"], str((self.root / "Codex/state/credentials.json").resolve()))
        self.assertEqual(loaded["publication_backend"]["auth_token_path"], str((self.root / "Codex/state/backend/auth-token.json").resolve()))

    def test_absolute_configuration_remains_supported(self):
        initialize_config(self.root, self.profile)
        data = json.loads(self.profile.read_text(encoding="utf-8"))
        data["skill_dir"] = str(BUNDLED_SKILL_DIR)
        data["output_dir"] = str(self.root / "custom-output")
        data["publication_backend"] = {"auth_token_path": (self.root / "private-backend/auth-token.json").as_posix()}
        data["sources"] = [{"id": "example", "refresh": {"credentials_path": (self.root / "private-source/credentials.json").as_posix()}}]
        self.profile.write_text(json.dumps(data), encoding="utf-8")
        loaded = load_config(self.profile)
        self.assertEqual(loaded["skill_dir"], str(BUNDLED_SKILL_DIR))
        self.assertEqual(loaded["output_dir"], data["output_dir"])
        self.assertEqual(loaded["publication_backend"], data["publication_backend"])
        self.assertEqual(loaded["sources"], data["sources"])

    def test_cli_init_needs_no_existing_profile_and_refuses_overwrite(self):
        with contextlib.redirect_stdout(io.StringIO()) as output:
            code = main(["init", "--workspace", str(self.root), "--output", str(self.profile)])
        self.assertEqual(code, 0)
        self.assertFalse(json.loads(output.getvalue())["publish_enabled"])
        with contextlib.redirect_stdout(io.StringIO()) as output:
            code = main(["init", "--workspace", str(self.root), "--output", str(self.profile)])
        self.assertEqual(code, 1)
        self.assertEqual(json.loads(output.getvalue())["error_type"], "FileExistsError")

    def test_draft_can_inherit_configured_author_without_mutating_input(self):
        raw = {"title": "离线测试", "body": "自有测试文本", "quotes": ["这是明确标记的离线测试句子"],
               "sources": [{"url": "https://example.com/fixture", "coverage": "full"}]}
        content, warnings = normalize_draft(raw, BUNDLED_SKILL_DIR, "本地作者")
        self.assertEqual(content["author"], "本地作者")
        self.assertNotIn("author", raw)
        raw["author"] = "其他作者"
        with self.assertRaises(ValueError):
            normalize_draft(raw, BUNDLED_SKILL_DIR, "本地作者")

    def test_explicit_font_takes_precedence_over_environment(self):
        generator = generator_module(BUNDLED_SKILL_DIR)
        fonts = generator.resolve_fonts()
        with patch.dict(os.environ, {"XHS_CJK_FONT": str(self.root / "missing.ttf")}):
            with self.assertRaises(ValueError):
                generator.resolve_fonts()
            selected = generator.resolve_fonts(cjk=fonts["cjk"], latin=fonts["latin"])
        self.assertEqual(selected, fonts)


class PublicExportTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="xhs-public-export-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name) / "source"
        self.root.mkdir()
        self.manifest = self.root / "public-files.txt"
        self.manifest.write_text("public-files.txt\nREADME.md\n", encoding="utf-8")
        (self.root / "README.md").write_text("自有说明 English 123\n", encoding="utf-8")
        self.archive = self.root.parent / "release.zip"

    def test_release_includes_only_allowlist_and_verified_manifest(self):
        (self.root / "cookies.json").write_text("PRIVATE_SESSION_NOT_FOR_EXPORT", encoding="utf-8")
        (self.root / "unrelated.txt").write_text("PRIVATE_CONTENT_NOT_FOR_EXPORT", encoding="utf-8")
        result = export_public(self.root, self.archive)
        self.assertTrue(result["crc_and_hashes_passed"])
        with ZipFile(self.archive) as archive:
            self.assertEqual(set(archive.namelist()), {"xhs-post-pipeline/README.md", "xhs-post-pipeline/public-files.txt", "xhs-post-pipeline/RELEASE_MANIFEST.json"})
            self.assertEqual(archive.read("xhs-post-pipeline/README.md"), (self.root / "README.md").read_bytes())
            metadata = json.loads(archive.read("xhs-post-pipeline/RELEASE_MANIFEST.json"))
            self.assertFalse(metadata["private_runtime_included"])

    def test_existing_release_is_preserved(self):
        self.archive.write_bytes(b"existing release")
        with self.assertRaises(FileExistsError):
            export_public(self.root, self.archive)
        self.assertEqual(self.archive.read_bytes(), b"existing release")

    def test_private_and_unsafe_manifest_entries_are_rejected(self):
        for name in ("../secret.txt", "/absolute/path", "C:/secret.txt", "Codex/state/credentials.json",
                     "cookies.json", "cookies/session-key.txt", "output/private.md", "config/private.json",
                     "pipeline/config.py", ".env.local", "README.md/../secret", "README.md\\extra"):
            with self.subTest(name=name):
                self.manifest.write_text("public-files.txt\n" + name + "\n", encoding="utf-8")
                with self.assertRaises(ValueError):
                    read_public_files(self.root)

    def test_case_collisions_are_rejected(self):
        self.manifest.write_text("public-files.txt\nREADME.md\nreadme.md\n", encoding="utf-8")
        with self.assertRaises(ValueError):
            read_public_files(self.root)

    def test_linked_files_are_rejected(self):
        external = self.root.parent / "private.txt"
        external.write_text("private", encoding="utf-8")
        try:
            (self.root / "linked.md").symlink_to(external)
        except OSError:
            self.skipTest("This Windows environment cannot create a symbolic link")
        self.manifest.write_text("public-files.txt\nlinked.md\n", encoding="utf-8")
        with self.assertRaises(ValueError):
            read_public_files(self.root)

    def test_secret_findings_do_not_echo_the_matched_value(self):
        secret = "sk-" + "a" * 32
        findings = scan_bytes("settings.py", secret.encode())
        self.assertEqual(findings[0]["reason"], "api_token")
        self.assertNotIn(secret, json.dumps(findings))
        (self.root / "README.md").write_text(secret, encoding="utf-8")
        self.assertFalse(audit(self.root)["success"])
        with self.assertRaises(ValueError):
            export_public(self.root, self.archive)
        self.assertFalse(self.archive.exists())

    def test_literal_credentials_outside_fixtures_are_rejected(self):
        findings = scan_bytes("settings.json", json.dumps({"password": "do-not-use-this-password"}).encode())
        self.assertEqual(findings[0]["reason"], "credential_literal")

    def test_encoding_artifacts_are_reported(self):
        self.assertEqual(scan_bytes("source.py", b"\xff")[0]["reason"], "not_utf8")
        self.assertEqual(scan_bytes("source.py", "\ufeffhello".encode())[0]["reason"], "encoding_artifact")

    def test_git_audit_checks_staged_bytes_not_only_working_files(self):
        subprocess.run(["git", "-C", str(self.root), "init", "-b", "main"], check=True, capture_output=True)
        secret = "sk-" + "b" * 32
        (self.root / "README.md").write_text(secret, encoding="utf-8")
        subprocess.run(["git", "-C", str(self.root), "add", "public-files.txt", "README.md"], check=True, capture_output=True)
        (self.root / "README.md").write_text("safe public content", encoding="utf-8")
        self.assertTrue(audit(self.root)["success"])
        result = audit(self.root, check_git=True)
        self.assertFalse(result["success"])
        self.assertTrue(any(item["reason"] == "git_index_api_token" for item in result["findings"]))


if __name__ == "__main__":
    unittest.main()
