"""Contracts for native Image assets. No model calls or automatic visual passes."""
import hashlib
import json
from pathlib import Path
from PIL import Image, ImageOps

ROLES = ("cover", "card_0", "card_1", "card_2", "card_3")
STYLE_DIRECTIONS = {
    "warm_herbal": "暖木色、米白、琥珀色，天然木材与草木静物，柔和侧光",
    "light_food": "奶白、浅绿、琥珀色，透明茶具与干净桌面，明亮自然柔光",
    "quiet_living": "奶油白、低饱和夜蓝、暖金色，棉麻与浅木材，暖灯和夜窗冷色",
}
INSPECTION_CHECKS = ("text_exact", "signature", "no_extra_text", "content_consistent",
                     "composition", "series_style", "mobile_readable", "safe_adaptation")


def digest(value):
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True,
                                    separators=(",", ":")).encode("utf-8")).hexdigest()


def file_hash(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def normalize_visual(value, quotes, text_value):
    if not isinstance(value, dict):
        raise ValueError("visual must be an object")
    allowed = {"version", "mode", "style_id", "cover", "cards", "palette", "lighting",
               "materials", "constraints", "signature_position"}
    if set(value) - allowed or type(value.get("version")) is not int or value["version"] != 1:
        raise ValueError("Unknown visual fields or visual.version; expected version 1")
    if value.get("mode") != "imagegen_native":
        raise ValueError("This version supports imagegen_native only; no silent typesetting fallback")
    if value.get("style_id") not in STYLE_DIRECTIONS or len(quotes) != 4:
        raise ValueError("Native five-image content requires a known style_id and four quotes")
    cover = value.get("cover")
    cover_fields = {"headline", "subheadline", "label", "scene", "text_region"}
    if not isinstance(cover, dict) or set(cover) - cover_fields:
        raise ValueError("Invalid visual.cover")
    cleaned_cover = {"headline": text_value(cover.get("headline"), "visual.cover.headline", single_line=True)}
    for name in cover_fields - {"headline"}:
        if name in cover:
            cleaned_cover[name] = text_value(cover[name], "visual.cover." + name,
                                            allow_empty=True, single_line=True)
    cards = value.get("cards")
    if not isinstance(cards, list) or len(cards) != 4:
        raise ValueError("visual.cards must contain exactly four card plans")
    cleaned_cards = []
    for card in cards:
        if not isinstance(card, dict) or set(card) - {"quote_index", "purpose", "scene", "text_region", "composition"}:
            raise ValueError("Invalid visual card fields")
        index = card.get("quote_index")
        if type(index) is not int or index not in range(4):
            raise ValueError("quote_index must be an integer from 0 to 3")
        cleaned = {"quote_index": index}
        for name in ("purpose", "scene", "text_region"):
            cleaned[name] = text_value(card.get(name), "visual.cards." + name, single_line=True)
        if "composition" in card:
            cleaned["composition"] = text_value(card["composition"], "composition", single_line=True)
        cleaned_cards.append(cleaned)
    if {card["quote_index"] for card in cleaned_cards} != set(range(4)):
        raise ValueError("Four quote_index values must be complete and unique")
    result = {"version": 1, "mode": "imagegen_native", "style_id": value["style_id"],
              "cover": cleaned_cover, "cards": sorted(cleaned_cards, key=lambda card: card["quote_index"])}
    for name in allowed - {"version", "mode", "style_id", "cover", "cards"}:
        if name in value:
            result[name] = text_value(value[name], "visual." + name, single_line=True)
    return result


def expected_text(content, role):
    if role == "cover":
        cover = content["visual"]["cover"]
        texts = [cover.get(name, "") for name in ("headline", "subheadline", "label")]
        return [text for text in texts if text] + ([content["author"]] if content["author"] else [])
    return [content["quotes"][int(role[-1])]] + ([content["author"]] if content["author"] else [])


def build_prompt(content, role):
    visual = content["visual"]
    cover = visual["cover"]
    common = ["Use case: ads-marketing", "用途：小红书原创完整成图，3:4竖版，手机阅读。",
              "主题：" + content["title"],
              "参考图仅参考光线、材质、配色和信息层级；不得复用参考图里的文字或功效。",
              "统一风格：" + STYLE_DIRECTIONS[visual["style_id"]]]
    for name in ("palette", "lighting", "materials", "constraints"):
        if visual.get(name):
            common.append(name + "：" + visual[name])
    if role == "cover":
        common += ["画面：" + cover.get("scene", "以正文支持的主题主体或生活场景为视觉核心。"),
                   "构图：中下部突出主体，上方标题区；" + cover.get("text_region", "上方自然留白"),
                   "主标题1至2行，大字清楚，细描边可用；副标题与标签较小，层级分明。"]
    else:
        card = visual["cards"][int(role[-1])]
        common += ["本卡职责：" + card["purpose"], "画面：" + card["scene"],
                   "文字区域：" + card["text_region"],
                   "视角与主体位置：" + card.get("composition", "与其他卡片有区别"),
                   "沿用本篇固定封面参考的光线、配色、材质，不复用封面字稿。",
                   "金句按语义分为2至3行，主句占上半页，文字与画面融为一体，背景细节不穿过文字。"]
    common += ["准确文字（每项仅出现一次，逐字逐标点保留，禁止增删）：" +
               json.dumps(expected_text(content, role), ensure_ascii=False),
               "字体请求：清晰的宋体风格中文印刷字形；英文数字使用Times New Roman风格。",
               "署名安全位置：" + visual.get("signature_position", "底部居中，距四边保留足够空间"),
               "四边保留约8%安全空间；只输出一张完整图片，不要拼图、网格、水印、二维码或额外装饰文字。",
               "不得添加功效、用量、时间承诺、认证、药物包装、解剖标签或正文没有的食材与操作步骤。"]
    return "\n".join(common)


def adapt_image(original, destination, thumbnail):
    """Keep every pixel of the composition; never crop text or stretch subjects."""
    with Image.open(original) as image:
        image.load()
        image = ImageOps.exif_transpose(image).convert("RGB")
        original_size = list(image.size)
        sample = image.resize((1, 1)).getpixel((0, 0))
        normalized = ImageOps.pad(image, (1080, 1440), method=Image.Resampling.LANCZOS,
                                  color=sample, centering=(0.5, 0.5))
        normalized.save(destination, "JPEG", quality=95, subsampling=0)
        normalized.resize((270, 360), Image.Resampling.LANCZOS).save(thumbnail, "JPEG", quality=93)
    return {"original_size": original_size, "output_size": [1080, 1440],
            "method": "contain_pad_no_crop_no_stretch", "padding_color": list(sample)}


def verify_inspection(record, content):
    inspection = record.get("inspection") or {}
    if (inspection.get("passed") is not True or inspection.get("issues") != []
            or inspection.get("result_hash") != record.get("normalized_sha256")
            or inspection.get("original_hash") != record.get("original_sha256")
            or inspection.get("thumbnail_hash") != record.get("thumbnail_sha256")
            or inspection.get("expected_text") != expected_text(content, record["role"])
            or inspection.get("observed_text") != expected_text(content, record["role"])):
        raise ValueError("Image inspection is missing or belongs to another result/text version")
    checks = inspection.get("checks", {})
    if not all(checks.get(name) is True for name in INSPECTION_CHECKS):
        raise ValueError("Every original/mobile image inspection check must pass explicitly")
    if not inspection.get("original_observation") or not inspection.get("mobile_observation"):
        raise ValueError("Actual original and mobile observations are required")


def verify_result(record, content, require_inspection=True):
    if record.get("role") not in ROLES or record.get("creative_hash") != digest(content):
        raise ValueError("Image belongs to another content/visual version")
    if not record.get("intent_id") or not record.get("cache_key"):
        raise ValueError("Generation intent and cache key required")
    prompt = record.get("prompt")
    if not isinstance(prompt, str) or record.get("prompt_hash") != hashlib.sha256(prompt.encode("utf-8")).hexdigest():
        raise ValueError("Prompt hash mismatch")
    receipt = record.get("receipt") or {}
    if (receipt.get("tool") != "image_gen.imagegen" or receipt.get("status") != "returned"
            or receipt.get("intent_id") != record["intent_id"]
            or receipt.get("original_sha256") != record.get("original_sha256")
            or not receipt.get("returned_metadata") or not receipt.get("called_at") or not receipt.get("returned_at")):
        raise ValueError("Actual native Image receipt is required; placeholder/fallback is not accepted")
    for name in ("original", "normalized", "thumbnail"):
        path = Path(record.get(name + "_path", ""))
        if not path.is_absolute() or not path.is_file() or file_hash(path) != record.get(name + "_sha256"):
            raise ValueError("Image result file/hash missing or changed: " + name)
        with Image.open(path) as picture:
            picture.load()
            if name != "original" and (picture.mode != "RGB" or picture.size != ((1080, 1440) if name == "normalized" else (270, 360))):
                raise ValueError("Incorrect normalized image/thumbnail dimensions or mode")
    for reference in record.get("references", []):
        if file_hash(reference["path"]) != reference["sha256"]:
            raise ValueError("Reference image changed after generation")
    if require_inspection:
        verify_inspection(record, content)


def verify_results(records, content):
    if not isinstance(records, list) or [record.get("role") for record in records] != list(ROLES):
        raise ValueError("Expected ordered native images: cover, card_0, card_1, card_2, card_3")
    if len({record["intent_id"] for record in records}) != 5:
        raise ValueError("Each image requires its own native tool call intent")
    for record in records:
        verify_result(record, content)
