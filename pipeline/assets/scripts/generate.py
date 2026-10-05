"""Create a local XHS material bundle from a UTF-8 JSON file. No network or publishing."""
import argparse
from datetime import datetime, timedelta, timezone
import hashlib
import json
from pathlib import Path
import re
import copy
import shutil
import sys
import tempfile
from urllib.parse import urlparse

try:
    import PIL
    from PIL import Image
    if __package__:
        from .image_assets import (ROLES, digest, file_hash, normalize_visual, verify_results)
        from .card_renderer import (CARD_HEIGHT, CARD_WIDTH, DEFAULT_AUTHOR, STYLES,
                                   FontPair, create_contact_sheet, create_quote_card, resolve_fonts)
    else:
        from image_assets import (ROLES, digest, file_hash, normalize_visual, verify_results)
        from card_renderer import (CARD_HEIGHT, CARD_WIDTH, DEFAULT_AUTHOR, STYLES,
                                   FontPair, create_contact_sheet, create_quote_card, resolve_fonts)
except ImportError as error:
    print(json.dumps({"success": False, "error": str(error),
        "next_step": "Install the project with pip install . to include Pillow."}), file=sys.stderr)
    raise SystemExit(1)

LOCAL_TIMEZONE = timezone(timedelta(hours=8), "Asia/Singapore")
FIELDS = {"title", "candidate_titles", "body", "tags", "quotes", "style", "author",
          "sources", "editorial_notes", "claim_checks", "fonts", "visual"}


def text_value(value, name, allow_empty=False, single_line=False):
    if not isinstance(value, str):
        raise ValueError(f"{name} must be a string.")
    if single_line and any(character in value for character in "\n\r\t"):
        raise ValueError(f"{name} must be a single line.")
    if any(ord(character) < 32 and character not in "\n\r\t" for character in value):
        raise ValueError(f"{name} contains control characters.")
    cleaned = value.strip()
    if not cleaned and not allow_empty:
        raise ValueError(f"{name} must not be empty.")
    return cleaned


def string_array(value, name):
    if not isinstance(value, list):
        raise ValueError(f"{name} must be an array of strings.")
    return [text_value(item, f"{name}[{index}]") for index, item in enumerate(value)]


def valid_url(value, name, allow_empty=False):
    url = text_value(value, name, allow_empty=allow_empty, single_line=True)
    if url and (urlparse(url).scheme not in ("http", "https") or not urlparse(url).netloc):
        raise ValueError(f"{name} must be an HTTP(S) URL.")
    return url


def normalize_content(raw):
    if not isinstance(raw, dict):
        raise ValueError("The input must be a JSON object.")
    unknown = set(raw) - FIELDS
    if unknown:
        raise ValueError(f"Unknown input fields: {', '.join(sorted(unknown))}")
    content, warnings = {}, []
    content["title"] = text_value(raw.get("title"), "title", single_line=True)
    if len(content["title"]) > 20:
        raise ValueError("Title exceeds 20 Unicode code points. Rewrite it; it will not be truncated.")
    candidates = string_array(raw.get("candidate_titles", [content["title"]]), "candidate_titles")
    for candidate in candidates:
        text_value(candidate, "candidate title", single_line=True)
        if len(candidate) > 20:
            raise ValueError("A candidate title exceeds 20 Unicode code points.")
    content["candidate_titles"] = candidates
    if len(candidates) != 3:
        warnings.append("默认三条候选标题；当前数量不同，请结合用户要求确认。")
    content["body"] = text_value(raw.get("body"), "body")
    body_characters = len(re.sub(r"\s", "", content["body"]))
    if not 400 <= body_characters <= 600:
        warnings.append(f"正文 {body_characters} 个非空白字符，偏离默认 400–600；请按实际任务审阅。")
    tags = string_array(raw.get("tags", []), "tags")
    tags = [tag.lstrip("#") for tag in tags]
    if any(not tag or re.search(r"[\s#]", tag) for tag in tags):
        raise ValueError("Tags must be nonempty names without whitespace or embedded #.")
    if len(tags) != len(set(tags)):
        raise ValueError("Tags must not contain duplicates.")
    content["tags"] = tags
    if not 6 <= len(tags) <= 8:
        warnings.append("话题数量偏离默认 6–8 个；请结合内容和用户要求审阅。")
    quotes = string_array(raw.get("quotes"), "quotes")
    if not 1 <= len(quotes) <= 9:
        raise ValueError("Supply between 1 and 9 card texts.")
    for index, quote in enumerate(quotes):
        if any(ord(character) >= 0x1F000 and not (0x20000 <= ord(character) <= 0x323AF)
               or ord(character) in (0xFE0E, 0xFE0F, 0x200D) for character in quote):
            raise ValueError("Emoji are not supported in text cards; put them in the post body.")
        count = len(re.sub(r"\s", "", quote))
        if not 15 <= count <= 30:
            warnings.append(f"卡片 {index+1} 为 {count} 字，偏离默认 15–30；文字不会被截断。")
    content["quotes"] = quotes
    if len(quotes) != 4:
        warnings.append("默认四张卡片；当前数量不同，请结合用户要求确认。")
    style = text_value(raw.get("style", "morandi"), "style", single_line=True)
    if style not in STYLES:
        raise ValueError(f"Unknown style: {style}")
    content["style"] = style
    content["author"] = text_value(raw.get("author", DEFAULT_AUTHOR), "author", allow_empty=True, single_line=True)
    fonts = raw.get("fonts", {})
    if not isinstance(fonts, dict) or set(fonts) - {"cjk", "latin"}:
        raise ValueError("fonts must be an object with optional cjk and latin paths.")
    for role, font_path in fonts.items():
        text_value(font_path, f"fonts.{role}", single_line=True)
        if not Path(font_path).is_absolute():
            raise ValueError(f"fonts.{role} must be an absolute path.")
    content["fonts"] = fonts
    sources = raw.get("sources", [])
    if not isinstance(sources, list) or any(not isinstance(source, dict) for source in sources):
        raise ValueError("sources must be an array of objects.")
    for index, source in enumerate(sources):
        for key in ("title", "author", "published_at", "fetched_at", "retrieval_method"):
            if key in source:
                text_value(source[key], f"sources[{index}].{key}", allow_empty=True, single_line=True)
        valid_url(source.get("url", ""), f"sources[{index}].url", allow_empty=True)
        if source.get("coverage") not in ("full", "partial", "user_text"):
            raise ValueError("Each source must specify coverage: full, partial, or user_text.")
        if source["coverage"] == "partial":
            warnings.append(f"来源 {index+1} 只有部分内容，不能声称已按全文改写。")
    content["sources"] = sources
    if not sources:
        warnings.append("没有来源记录；发布前补充自有文本说明或真实来源。")
    content["editorial_notes"] = string_array(raw.get("editorial_notes", []), "editorial_notes")
    claims = raw.get("claim_checks", [])
    if not isinstance(claims, list) or any(not isinstance(claim, dict) for claim in claims):
        raise ValueError("claim_checks must be an array of objects.")
    for index, claim in enumerate(claims):
        text_value(claim.get("claim"), f"claim_checks[{index}].claim")
        text_value(claim.get("reason"), f"claim_checks[{index}].reason")
        if claim.get("decision") not in ("retain", "qualify", "omit"):
            raise ValueError("Claim decision must be retain, qualify, or omit.")
        urls = string_array(claim.get("evidence_urls", []), "evidence_urls")
        for url in urls:
            valid_url(url, "evidence URL")
        if claim["decision"] == "retain" and not urls:
            warnings.append(f"论点 {index+1} 标为保留但未附证据 URL，请人工核查。")
    content["claim_checks"] = claims
    if "visual" in raw:
        content["visual"] = normalize_visual(raw["visual"], quotes, text_value)
    return content, warnings, body_characters


def safe_slug(title):
    slug = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", title).strip(" .")[:24]
    if not slug:
        slug = "note"
    if re.fullmatch(r"(?i)(CON|PRN|AUX|NUL|COM[1-9]|LPT[1-9])(?:\..*)?", slug):
        slug = "note_" + slug
    return slug


def write_json(path, value):
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def source_review(content, warnings):
    lines = ["# 来源与编辑审阅", "", "本文件供审阅；图片预览和本地生成状态不代表发布或论点核验已经完成。", "", "## 来源", ""]
    for index, source in enumerate(content["sources"], 1):
        lines.extend([f"{index}. {source.get('title') or '未填写标题'}", f"   - URL：{source.get('url') or '用户文本或未填写'}",
            f"   - 作者：{source.get('author') or '未填写'}", f"   - 日期：{source.get('published_at') or '未核实'}",
            f"   - 获取：{source.get('retrieval_method') or '未填写'}；覆盖：{source['coverage']}"])
    if not content["sources"]:
        lines.append("未提供来源记录。")
    lines.extend(["", "## 编辑说明", ""])
    lines.extend("- " + note for note in content["editorial_notes"])
    if not content["editorial_notes"]:
        lines.append("无额外说明。")
    lines.extend(["", "## 论点处理", ""])
    for claim in content["claim_checks"]:
        lines.extend([f"- {claim['claim']}：{claim['decision']}", f"  - 理由：{claim['reason']}"])
        lines.extend("  - 证据：" + url for url in claim.get("evidence_urls", []))
    if not content["claim_checks"]:
        lines.append("未提供逐项论点记录；脚本不自动判断真实性。")
    lines.extend(["", "## 生成提示", ""])
    lines.extend("- " + warning for warning in warnings)
    if not warnings:
        lines.append("默认字数与数量检查通过；仍需内容和视觉审阅。")
    return "\n".join(lines) + "\n"


def default_output_root():
    return Path.cwd() / "Codex" / "outputs" / "xhs-post"


def generate_bundle(content, warnings, body_characters, target, generated_at, image_results=None):
    if target.exists():
        raise ValueError(f"Output directory already exists; refusing to overwrite: {target}")
    target.parent.mkdir(parents=True, exist_ok=True)
    fonts = resolve_fonts(**content["fonts"])
    native = bool(content.get("visual"))
    if native:
        verify_results(image_results, content)
    elif image_results is not None:
        raise ValueError("Native image results require an explicit visual content protocol")
    stage = Path(tempfile.mkdtemp(prefix=".xhs-post-", dir=target.parent))
    try:
        tag_line = " ".join("#" + tag for tag in content["tags"])
        post = f"标题: {content['title']}\n\n{content['body']}"
        if tag_line:
            post += "\n\n" + tag_line
        (stage / "post.txt").write_text(post + "\n", encoding="utf-8")
        write_json(stage / "content.json", content)
        (stage / "source_review.md").write_text(source_review(content, warnings), encoding="utf-8")
        paths, layouts, publish_images = [], [], []
        if native:
            for order, record in enumerate(image_results):
                role = record["role"]
                path = stage / (role + ".jpg")
                shutil.copy2(record["normalized_path"], path)
                saved = copy.deepcopy(record)
                asset_root = stage / "originals" / role
                asset_root.mkdir(parents=True)
                for name in ("original", "thumbnail"):
                    source = Path(record[name + "_path"])
                    relative = Path("originals") / role / (name + source.suffix.lower())
                    shutil.copy2(source, stage / relative)
                    saved[name + "_path"] = str(target / relative)
                saved["normalized_path"] = str(target / path.name)
                for index, reference in enumerate(saved.get("references", [])):
                    source = Path(reference["path"])
                    relative = Path("originals") / role / ("reference_" + str(index) + source.suffix.lower())
                    shutil.copy2(source, stage / relative)
                    reference["path"] = str(target / relative)
                write_json(asset_root / "receipt.json", saved["receipt"])
                (asset_root / "prompt.txt").write_text(saved["prompt"] + "\n", encoding="utf-8")
                publish_images.append({"asset_id": role, "role": role, "order": order,
                                       "path": str(target / path.name), "width": 1080, "height": 1440,
                                       "sha256": file_hash(path), "generation": saved})
                paths.append(path)
                layouts.append({"role": role, "text_source": "cover" if role == "cover" else "quotes[" + role[-1] + "]",
                                "glyphs": "generative_requested_songti_tnr_not_font_file_verified",
                                "inspection": saved["inspection"]})
            write_json(stage / "images.json", publish_images)
        for index, quote in enumerate(content["quotes"] if not native else []):
            path = stage / f"card_{index}.jpg"
            layout = create_quote_card(quote, path, content["style"], content["author"], fonts)
            with Image.open(path) as image:
                image.load()
                if image.size != (CARD_WIDTH, CARD_HEIGHT) or image.mode != "RGB":
                    raise ValueError(f"Unexpected image dimensions or mode: {path.name}")
            paths.append(path)
            layouts.append(layout)
        labels = ["1 封面", "2 核心认识", "3 解释", "4 做法", "5 提醒"] if native else None
        sheet_size = create_contact_sheet(paths, stage / "contact_sheet.jpg", labels=labels, fonts=fonts)
        with Image.open(stage / "contact_sheet.jpg") as sheet:
            sheet.load()
        files = []
        for path in sorted(path for path in stage.rglob("*") if path.is_file()):
            relative = path.relative_to(stage)
            files.append({"name": relative.as_posix(), "path": str(target/relative), "bytes": path.stat().st_size,
                          "sha256": hashlib.sha256(path.read_bytes()).hexdigest()})
        manifest = {"schema_version": 2 if native else 1, "status": "materials_generated", "published": False,
            "generated_at": generated_at.isoformat(), "title": content["title"], "style": content["style"],
            "author": content["author"], "body_characters_without_whitespace": body_characters,
            "output_dir": str(target), "post_file": str(target/"post.txt"),
            "images": [str(target/path.name) for path in paths], "contact_sheet": str(target/"contact_sheet.jpg"),
            "contact_sheet_size": sheet_size, "files": files, "layouts": layouts, "warnings": warnings,
            "checks": {"images_fully_decoded": True, "image_size": [CARD_WIDTH, CARD_HEIGHT],
                       "all_text_retained": True, "text_inside_bounds": True,
                       "visual_review": "pending", "source_claim_review": "agent_or_user_required"}}
        if native:
            manifest.update(protocol="imagegen_native_five", creative_hash=digest(content), publish_images=publish_images,
                            requested_model_family="gpt-image-2.5", observed_model=None,
                            font_provenance="generative_glyphs_requested_style_not_font_files")
            manifest["checks"]["all_text_retained"] = all(r["generation"]["inspection"]["checks"]["text_exact"] for r in publish_images)
            manifest["checks"]["text_inside_bounds"] = all(r["generation"]["inspection"]["checks"]["safe_adaptation"] for r in publish_images)
        write_json(stage / "_meta.json", manifest)
        if target.exists():
            raise ValueError("The output directory appeared during generation; refusing to overwrite.")
        stage.rename(target)
        return {"success": True, "status": manifest["status"], "output_dir": str(target),
                "title": content["title"], "post_file": manifest["post_file"], "images": manifest["images"],
                "contact_sheet": manifest["contact_sheet"], "meta_file": str(target/"_meta.json"),
                "warnings": warnings, "published": False}
    except Exception as error:
        if stage.exists():
            write_json(stage / "_failure.json", {"success": False, "error": str(error), "target": str(target)})
        raise ValueError(f"{error} Diagnostic directory: {stage}") from error


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", type=Path, help="UTF-8 content JSON")
    output = parser.add_mutually_exclusive_group()
    output.add_argument("--output-root", type=Path, help="Parent for a new timestamped bundle")
    output.add_argument("--output-dir", type=Path, help="Exact new directory; existing paths are refused")
    parser.add_argument("--doctor", action="store_true", help="Inspect dependencies and fonts; do not create files")
    parser.add_argument("--image-results", type=Path, help="Ordered registered and inspected native Image result JSON")
    args = parser.parse_args()
    try:
        if args.doctor:
            fonts = resolve_fonts()
            pair = FontPair(40, fonts)
            pair.validate("中文字体 English 123")
            print(json.dumps({"success": True, "python": sys.executable, "pillow": PIL.__version__,
                "font_paths": fonts, "font_families": {"cjk": pair.cjk.getname()[0], "latin": pair.latin.getname()[0]},
                "styles": list(STYLES), "network_required": False}, ensure_ascii=False))
            return 0
        if not args.input:
            parser.error("--input is required unless --doctor is used")
        raw = json.loads(args.input.resolve().read_text(encoding="utf-8-sig"))
        content, warnings, body_characters = normalize_content(raw)
        now = datetime.now(LOCAL_TIMEZONE)
        if args.output_dir:
            target = args.output_dir.resolve()
        else:
            root = (args.output_root or default_output_root()).resolve()
            target = root / f"{now.strftime('%Y%m%d_%H%M%S_%f')}_{safe_slug(content['title'])}"
        image_results = json.loads(args.image_results.read_text(encoding="utf-8-sig")) if args.image_results else None
        result = generate_bundle(content, warnings, body_characters, target, now, image_results)
        print(json.dumps(result, ensure_ascii=False))
        return 0
    except (OSError, ValueError) as error:
        print(json.dumps({"success": False, "error": str(error)}, ensure_ascii=False), file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
