"""Configuration contains policies and paths, never account secrets."""
from datetime import datetime, timezone, timedelta
import hashlib
import json
from pathlib import Path

TZ = timezone(timedelta(hours=8), "Asia/Singapore")
BUNDLED_SKILL_DIR = Path(__file__).resolve().parents[1] / "assets"


def now_iso():
    return datetime.now(TZ).isoformat(timespec="seconds")


def digest(value):
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True,
                                    separators=(",", ":")).encode("utf-8")).hexdigest()


def read_json(path):
    return json.loads(Path(path).read_text(encoding="utf-8-sig"))


def write_json(path, value):
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = target.with_suffix(target.suffix + ".tmp")
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    temporary.replace(target)


def load_config(path):
    target = Path(path).resolve()
    data = read_json(target)
    if data.get("schema_version") != 2:
        raise ValueError("Configuration schema_version must be 2")
    workspace_value = data.get("workspace", "")
    if not isinstance(workspace_value, str) or not workspace_value.strip():
        raise ValueError("workspace must identify an existing directory")
    workspace = Path(workspace_value).expanduser()
    if not workspace.is_absolute():
        workspace = target.parent / workspace
    workspace = workspace.resolve()
    if not workspace.is_dir():
        raise ValueError("workspace must identify an existing directory")
    if data.get("timezone", "Asia/Singapore") != "Asia/Singapore":
        raise ValueError("This deployment supports the configured UTC+08 timezone only")
    sources = data.get("sources", [])
    if not isinstance(sources, list) or any(not isinstance(s, dict) or not s.get("id") for s in sources):
        raise ValueError("sources must be a list of objects with stable id")
    if len({s["id"] for s in sources}) != len(sources):
        raise ValueError("source ids must be unique")
    editorial = data.get("editorial", {})
    if not isinstance(editorial, dict) or ("require_source_fidelity" in editorial
            and type(editorial["require_source_fidelity"]) is not bool):
        raise ValueError("editorial.require_source_fidelity must be an explicit boolean")
    policy = data.get("policy", {})
    if type(policy.get("max_posts_per_day", 1)) is not int or not 1 <= policy.get("max_posts_per_day", 1) <= 20:
        raise ValueError("max_posts_per_day must be an integer between 1 and 20")
    if policy.get("max_revision_rounds", 2) not in range(1, 6):
        raise ValueError("max_revision_rounds must be between 1 and 5")
    images = data.get("image_policy", {})
    if not isinstance(images, dict) or set(images) - {"default_mode", "require_five_images", "max_repairs_per_image", "requested_model_family"}:
        raise ValueError("Invalid image_policy fields")
    if images:
        if (images.get("default_mode") != "imagegen_native" or type(images.get("require_five_images")) is not bool
                or type(images.get("max_repairs_per_image", 2)) is not int or not 0 <= images.get("max_repairs_per_image", 2) <= 5
                or images.get("requested_model_family", "gpt-image-2.5") != "gpt-image-2.5"):
            raise ValueError("Invalid native Image policy")
    for key, default in (("state_dir", "Codex/state/xhs-post"), ("work_dir", "Codex/work/xhs-post"),
                         ("output_dir", "output")):
        value = Path(data.get(key, default))
        data[key] = str(value if value.is_absolute() else workspace / value)
    skill = Path(data.get("skill_dir") or BUNDLED_SKILL_DIR).expanduser()
    data["skill_dir"] = str((skill if skill.is_absolute() else workspace / skill).resolve())
    for source in sources:
        for key in ("path", "html_path"):
            if source.get(key):
                value = Path(source[key]).expanduser()
                if not value.is_absolute():
                    source[key] = str((workspace / value).resolve())
        refresh = source.get("refresh", {})
        if refresh.get("credentials_path"):
            value = Path(refresh["credentials_path"]).expanduser()
            if not value.is_absolute():
                refresh["credentials_path"] = str((workspace / value).resolve())
    backend = data.get("publication_backend", {})
    if backend.get("auth_token_path"):
        value = Path(backend["auth_token_path"]).expanduser()
        if not value.is_absolute():
            backend["auth_token_path"] = str((workspace / value).resolve())
    data["workspace"] = str(workspace)
    data["config_path"] = str(target)
    data["policy_hash"] = digest({"account": data.get("account", {}), "policy": policy,
                                  "sources": sources, "editorial": data.get("editorial", {}),
                                  "publication_backend": data.get("publication_backend", {}),
                                  **({"image_policy": images} if images else {})})
    return data


def initialize_config(workspace, output, author=""):
    """Create a new private profile with discovery and publication disabled."""
    workspace = Path(workspace).expanduser().resolve()
    if not workspace.is_dir():
        raise ValueError("workspace must identify an existing directory")
    output = Path(output).expanduser().resolve()
    data = {
        "schema_version": 2, "workspace": str(workspace),
        "timezone": "Asia/Singapore", "engine": "codex", "author": author,
        "state_dir": "Codex/state/xhs-post", "work_dir": "Codex/work/xhs-post",
        "output_dir": "output", "sources": [],
        "account": {"nickname": "", "account_id": "", "user_id": ""},
        "policy": {"allow_publish": False, "submit_backend_ready": False,
                   "max_posts_per_day": 1, "max_revision_rounds": 2,
                   "max_acquire_per_run": 3, "publish_hours": [9, 21]},
        "publication_backend": {"type": "disabled", "reason": "Configure and verify a backend before enabling submission"},
        "editorial": {"excluded_source_names": [], "require_source_fidelity": True},
    }
    output.parent.mkdir(parents=True, exist_ok=True)
    with output.open("x", encoding="utf-8") as stream:
        json.dump(data, stream, ensure_ascii=False, indent=2)
        stream.write("\n")
    return {"config_path": str(output), "publish_enabled": False}
