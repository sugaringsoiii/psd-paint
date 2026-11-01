"""다른 페이지를 칠한 레퍼런스를 보고 원고의 영역마다 부위를 정한다 (AI 매칭).

1. 원고에서 칠할 영역에 번호를 붙인 지도 이미지를 만든다.
2. 레퍼런스에서 색(팔레트)을 뽑고 P1, P2 … 표시를 얹은 이미지를 만든다.
3. 두 이미지를 AI에 보여 주고 영역 번호마다 팔레트를 고르게 한다.
   - Claude API로 바로 묻거나,
   - 같은 내용을 폴더로 내보내 Claude Code·Codex 같은 도구에서 답(answer.json)을 받아 다시 적용한다.
"""
import base64
import io
import json
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path

import cv2
import numpy as np
from PIL import Image, ImageDraw, ImageFont
from scipy import ndimage

CHUNK_H = 1000       # 이미지 한 장의 최대 높이(px). 길게 이어진 웹툰 페이지는 나눠 보낸다
MARGIN = 30          # 조각을 내용에 맞춰 자를 때 남기는 여백
REF_WIDTH = 900      # 레퍼런스 이미지 폭
LABELS_PER_COLOR = 3  # 팔레트 색마다 표시를 얹을 덩어리 수

PROMPT = """웹툰 밑색 작업이다. 이미지 두 묶음을 준다.

[레퍼런스] 같은 작품의 다른 페이지를 칠해 둔 그림이다. 칠한 색마다 P1, P2 … 표시를 얹었다.
[원고 지도] 지금 칠할 페이지의 선화다. 칠할 영역마다 옅은 색과 숫자를 얹었다.

할 일: 원고 지도의 숫자 영역마다, 레퍼런스에서 같은 인물의 같은 부위에 쓰인 팔레트(P번호)를 고른다.

먼저 analysis에 짧게 적는다.
1. 레퍼런스 인물마다: 알아볼 특징(머리모양·머리띠·옷차림)과 부위별 팔레트 (예: 고양이 머리띠 소년 = 머리 P2, 머리띠 P5, 재킷 P3, 피부 P6)
2. 원고 지도 인물마다: 어느 레퍼런스 인물인지와 그 근거, 그리고 그 인물에 속한 번호들
그다음 assignments에 번호마다 팔레트를 적는다. analysis에 적은 대응과 어긋나지 않게 한다.
- 머리모양·액세서리·옷차림으로 같은 인물을 알아본다. 같은 인물이면 페이지가 달라도 같은 색을 쓴다.
- 피부(얼굴·목·손), 머리카락, 옷(재킷·셔츠·넥타이·치마·바지 등), 소품을 구분한다.
- 피부(얼굴·목·귀·손·팔·다리)는 레퍼런스에 없는 인물(엑스트라)이어도 반드시 피부 팔레트를 고른다. 피부를 none으로 두면 회색으로 칠해진다.
- 레퍼런스에 없는 인물의 머리카락·옷·사물이거나 판단이 서지 않으면 "none"을 고른다. 억지로 고르지 않는다.
- 배경(하늘·벽·바닥 등)은 "none"이다.
- 지도에서 바탕색이 같은 번호들은 선이 끊긴 틈으로 이어진 한 덩어리다 (옷 주름선 등으로 나뉜 같은 옷·같은 머리일 가능성이 크다). 특별한 이유가 없으면 같은 팔레트를 고른다. 단, 머리카락과 얼굴처럼 묶음 안에서도 부위가 분명히 다르면 따로 고른다.
- 지도의 모든 숫자에 대해 하나씩 답한다.
{palette_note}
팔레트: {palette_list}
영역 번호: 1 ~ {count}"""

REVIEW_PROMPT = """웹툰 밑색 검수다. 이미지 두 묶음을 준다.

[레퍼런스] 같은 작품의 다른 페이지를 칠해 둔 그림이다. 칠한 색마다 P1, P2 … 표시를 얹었다.
[칠한 결과] 이전 답대로 원고를 칠한 결과다. 영역마다 번호를 얹었다. 분홍색 영역은 none(나중에 회색으로 자동 칠함)이다.

할 일: 레퍼런스와 비교해 잘못 칠한 번호만 고친다.
- 같은 인물인데 머리·옷 색이 레퍼런스와 다른 곳 (인물을 헷갈려 색이 서로 바뀐 경우 포함)
- 피부(얼굴·목·귀·손·팔·다리)가 피부 팔레트가 아닌 곳. 엑스트라도 피부는 피부 팔레트다
- 머리카락·옷 덩어리 안에서 한 조각만 다른 색인 곳 (주름선으로 나뉜 조각은 같은 옷이다)
- 배경인데 칠한 곳은 none

먼저 analysis에 무엇이 틀렸는지 짧게 적고, assignments에는 고칠 번호만 새 팔레트로 적는다. 맞는 번호는 적지 않는다.
팔레트: {palette_list}
영역 번호: 1 ~ {count}"""

NAME_NOTE = """- layer_names에는 팔레트마다 밑색 레이어 이름을 짧은 한국어로 붙인다 (예: 머리, 피부, 재킷, 셔츠, 넥타이, 치마, 바지, 신발, 소품). 같은 부위면 인물이 달라도 같은 이름을 쓴다."""


@dataclass
class Job:
    candidates: np.ndarray            # 지도 번호 k(1부터) → 원고 영역 번호
    palette: list                     # [{"id": "P1", "rgb": (r, g, b), "name": str|None}]
    ref_images: list                  # PIL 이미지
    map_images: list                  # PIL 이미지
    prompt: str
    schema: dict
    usage: dict = field(default_factory=dict)
    palette_text: str = ""
    groups: np.ndarray = None   # 지도 번호 순서의 묶음 id (선이 끊겨 이어진 영역끼리 같다)
    areas: np.ndarray = None    # 지도 번호 순서의 영역 넓이


def _font(size):
    for path in ("/System/Library/Fonts/Supplemental/Arial Bold.ttf", "C:/Windows/Fonts/arialbd.ttf",
                 "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"):
        try:
            return ImageFont.truetype(path, size)
        except OSError:
            continue
    return ImageFont.load_default()


def _chunks(h, rows_busy, max_h=CHUNK_H):
    """세로로 긴 이미지를 max_h 이하로 나눌 위치. 내용이 적은 줄(컷 사이 여백)에서 자른다."""
    cuts, y = [0], 0
    while h - y > max_h:
        lo, hi = y + max_h // 2, y + max_h
        y = lo + int(np.argmin(rows_busy[lo:hi]))
        cuts.append(y)
    cuts.append(h)
    return list(zip(cuts[:-1], cuts[1:]))


def _pole(mask):
    d = ndimage.distance_transform_edt(np.pad(mask, 1))[1:-1, 1:-1]
    y, x = np.unravel_index(np.argmax(d), d.shape)
    return int(x), int(y)


def region_groups(labels, boundary, cand_ids):
    """선이 끊겨 원래 이어져 있던 영역끼리 묶는다 (옷 주름선 등으로 나뉜 같은 덩어리). → 번호 순서의 묶음 id

    영역은 끊긴 선을 메운 뒤 나눈 것이다. 메우기 전 선화로 다시 나누면 끊긴 틈으로 이어진 영역이 한 덩어리가 된다.
    """
    coarse, _ = ndimage.label(~boundary)
    of = np.zeros(labels.max() + 1, np.int64)
    m = labels > 0
    of[labels[m]] = coarse[m]
    return of[cand_ids]


def build_map(labels, boundary, cand_ids, colors=None, groups=None):
    """원고 지도: 칠할 영역마다 색과 번호. → (이미지 목록, 지도 번호 → 영역 번호 배열)

    colors가 없으면 묶음(groups)마다 옅은 임의의 색을 쓴다. 같은 묶음은 같은 바탕색이 된다.
    colors가 있으면 그 색(번호 순서, 칠한 결과)을 쓴다.
    """
    h, w = labels.shape
    lut = np.zeros(labels.max() + 1, np.int32)
    lut[cand_ids] = np.arange(1, len(cand_ids) + 1)
    kmap = lut[labels]
    if colors is None:
        if groups is None:
            groups = np.arange(len(cand_ids))
        _, gidx = np.unique(groups, return_inverse=True)
        colors = np.random.default_rng(0).integers(175, 245, (gidx.max() + 1, 3))[gidx]
    tint = np.vstack([[255, 255, 255], colors]).astype(np.uint8)
    rgb = tint[kmap]
    rgb[boundary] = 0
    img = Image.fromarray(rgb)
    draw = ImageDraw.Draw(img)
    font = _font(26)
    depth = ndimage.distance_transform_edt(kmap > 0)
    spots = ndimage.maximum_position(depth, kmap, index=np.arange(1, len(cand_ids) + 1))
    for k, (y, x) in enumerate(spots, 1):
        draw.text((x, y), str(k), fill=(210, 0, 0), font=font, anchor="mm", stroke_width=3, stroke_fill=(255, 255, 255))
    busy = (kmap > 0).sum(1)
    ys, _ = np.nonzero(kmap)
    top, bottom = (int(ys.min()), int(ys.max()) + 1) if ys.size else (0, h)
    # 칠할 영역이 없는 조각(컷 사이 여백)은 보내지 않고, 조각마다 내용 폭에 맞춰 잘라 글씨를 크게 보이게 한다
    images = []
    for a, b in _chunks(bottom - top, busy[top:bottom]):
        y0, y1 = top + a, top + b
        cols = np.nonzero((kmap[y0:y1] > 0).any(0))[0]
        if not cols.size:
            continue
        x0, x1 = max(0, int(cols.min()) - MARGIN), min(w, int(cols.max()) + 1 + MARGIN)
        images.append(img.crop((x0, y0, x1, y1)))
    return images, np.r_[0, cand_ids]


def build_reference(ref_rgb, palette_masks, palette):
    """레퍼런스 이미지에 팔레트 표시(P1 …)를 얹는다. 색마다 큰 덩어리 몇 개에 표시한다."""
    h, w = ref_rgb.shape[:2]
    scale = min(1.0, REF_WIDTH / w)
    small = Image.fromarray(ref_rgb).resize((round(w * scale), round(h * scale)), Image.LANCZOS)
    draw = ImageDraw.Draw(small)
    font = _font(18)
    for entry, mask in zip(palette, palette_masks):
        m = cv2.resize(mask.astype(np.uint8), small.size, interpolation=cv2.INTER_NEAREST) > 0
        lab, n = ndimage.label(m)
        if not n:
            continue
        sizes = np.bincount(lab.ravel())[1:]
        for i in np.argsort(-sizes)[:LABELS_PER_COLOR]:
            if sizes[i] < 150:
                break
            x, y = _pole(lab == i + 1)
            draw.text((x, y), entry["id"], fill=(220, 0, 0), font=font, anchor="mm", stroke_width=3,
                      stroke_fill=(255, 255, 255))
    arr = np.asarray(small.convert("L"))
    content = np.nonzero((arr < 245).any(1))[0]
    top, bottom = (int(content.min()), int(content.max()) + 1) if content.size else (0, small.height)
    busy = (arr < 245).sum(1)
    return [small.crop((0, top + a, small.width, top + b)) for a, b in _chunks(bottom - top, busy[top:bottom])
            if busy[top + a:top + b].any()]


def palette_from_layers(layers):
    """레퍼런스 PSD의 밑색 레이어들 → 팔레트(이름 있음)와 색별 마스크. layers = [(이름, RGBA 배열)]"""
    palette, masks = [], []
    for name, rgba in layers:
        solid = rgba[..., 3] >= 250
        if not solid.any():
            continue
        packed = (rgba[..., 0].astype(np.int32) << 16) | (rgba[..., 1].astype(np.int32) << 8) | rgba[..., 2]
        colors, counts = np.unique(packed[solid], return_counts=True)
        for c, cnt in zip(colors, counts):
            if cnt < 200:
                continue
            palette.append({"id": f"P{len(palette) + 1}", "rgb": ((c >> 16) & 255, (c >> 8) & 255, c & 255), "name": name})
            masks.append(solid & (packed == c))
    # 피부 색이 없으면 흰 피부를 넣는다. 엑스트라 피부가 회색으로 칠해지지 않게 한다
    if not any(p["name"] == "피부" for p in palette):
        palette.append({"id": f"P{len(palette) + 1}", "rgb": (255, 255, 255), "name": "피부"})
        masks.append(np.zeros(layers[0][1].shape[:2], bool) if layers else np.zeros((1, 1), bool))
    return palette, masks


def palette_from_image(rgba, quantize, tol, min_share):
    """칠한 예시 이미지 → 팔레트(이름 없음)와 색별 마스크. 선처럼 가는 색과 흰 배경은 뺀다."""
    solid = rgba[..., 3] >= 250
    idx, colors = quantize(rgba[..., :3][solid], tol, min_share)
    full = np.full(solid.shape, -1, np.int32)
    full[solid] = idx
    palette, masks = [], []
    k = cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (7, 7))
    has_white = False
    for j, rgb in enumerate(colors):
        m = full == j
        if min(rgb) >= 248 and m.mean() > 0.3:  # 페이지 바탕. 피부·흰 옷도 같은 흰색이라 아래에서 따로 넣는다
            has_white = True
            continue
        body = cv2.morphologyEx(m.astype(np.uint8), cv2.MORPH_OPEN, k) > 0
        if body.sum() < 0.4 * m.sum():  # 대부분 가는 선
            continue
        palette.append({"id": f"P{len(palette) + 1}", "rgb": tuple(rgb), "name": None})
        masks.append(body)
    if has_white or not any(min(p["rgb"]) >= 248 for p in palette):
        palette.append({"id": f"P{len(palette) + 1}", "rgb": (255, 255, 255), "name": None,
                        "note": "흰색: 피부·흰 셔츠 등 칠하지 않은 듯 하얀 부위"})
        masks.append(np.zeros(solid.shape, bool))  # 바탕과 구분되지 않으므로 그림에는 표시하지 않는다
    return palette, masks


def make_job(labels, boundary, cand_ids, ref_rgb, palette, masks):
    groups = region_groups(labels, boundary, cand_ids)
    map_images, cands = build_map(labels, boundary, cand_ids, groups=groups)
    ref_images = build_reference(ref_rgb, masks, palette)
    ids = [p["id"] for p in palette]
    named = all(p["name"] for p in palette)
    palette_list = ", ".join(f"{p['id']}=#{p['rgb'][0]:02X}{p['rgb'][1]:02X}{p['rgb'][2]:02X}"
                             + (f"({p['name'] or p.get('note')})" if p["name"] or p.get("note") else "")
                             for p in palette)
    prompt = PROMPT.format(palette_note="" if named else NAME_NOTE, palette_list=palette_list, count=len(cand_ids))
    props = {
        # 번호를 고르기 전에 인물 대응을 먼저 적게 해 인물을 헷갈리는 실수를 줄인다 (답은 이 순서로 나온다)
        "analysis": {"type": "string"},
        "assignments": {"type": "array", "items": {
            "type": "object",
            "properties": {"region": {"type": "integer"}, "palette": {"type": "string", "enum": ids + ["none"]}},
            "required": ["region", "palette"], "additionalProperties": False}},
    }
    if not named:
        props["layer_names"] = {"type": "array", "items": {
            "type": "object",
            "properties": {"palette": {"type": "string", "enum": ids}, "layer": {"type": "string"}},
            "required": ["palette", "layer"], "additionalProperties": False}}
    schema = {"type": "object", "properties": props, "required": list(props), "additionalProperties": False}
    areas = np.bincount(labels.ravel(), minlength=labels.max() + 1)[cand_ids]
    return Job(cands, palette, ref_images, map_images, prompt, schema, palette_text=palette_list,
               groups=groups, areas=areas)


def _b64(img):
    buf = io.BytesIO()
    img.convert("RGB").save(buf, "PNG", optimize=True)
    return base64.standard_b64encode(buf.getvalue()).decode()


def ask_claude(job, api_key, model):
    """Claude API로 영역마다 팔레트를 고른다. → 답 dict. 토큰 사용량은 job.usage에 남긴다."""
    import anthropic

    content = [{"type": "text", "text": "[레퍼런스]"}]
    content += [{"type": "image", "source": {"type": "base64", "media_type": "image/png", "data": _b64(im)}}
                for im in job.ref_images]
    content.append({"type": "text", "text": "[원고 지도]"})
    content += [{"type": "image", "source": {"type": "base64", "media_type": "image/png", "data": _b64(im)}}
                for im in job.map_images]
    content.append({"type": "text", "text": job.prompt})

    client = anthropic.Anthropic(api_key=api_key, timeout=600)
    # 안전 분류기가 요청을 거절하면 서버가 다른 모델로 이어서 처리하도록 기본 대체 설정을 켠다
    resp = client.beta.messages.create(
        model=model,
        max_tokens=16000,
        betas=["server-side-fallback-2026-07-01"],
        fallbacks="default",
        output_config={"format": {"type": "json_schema", "schema": job.schema}},
        messages=[{"role": "user", "content": content}],
    )
    job.usage = {"input": resp.usage.input_tokens, "output": resp.usage.output_tokens, "model": resp.model}
    if resp.stop_reason == "refusal":
        raise RuntimeError("AI가 요청을 거절했습니다")
    text = next(b.text for b in resp.content if b.type == "text")
    return json.loads(text)


def _post(url, headers, body, timeout=600):
    req = urllib.request.Request(url, data=json.dumps(body).encode(), headers={**headers, "Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return json.load(r)
    except urllib.error.HTTPError as e:
        detail = e.read().decode(errors="replace")[:500]
        raise RuntimeError(f"AI 요청 실패 ({e.code}): {detail}") from None
    except urllib.error.URLError as e:
        raise RuntimeError(f"AI 서버에 연결하지 못했습니다: {e.reason}") from None


def _strip_schema(node):
    """Gemini가 받지 않는 additionalProperties를 뺀 스키마."""
    if isinstance(node, dict):
        return {k: _strip_schema(v) for k, v in node.items() if k != "additionalProperties"}
    if isinstance(node, list):
        return [_strip_schema(v) for v in node]
    return node


def _openapi_schema(node):
    """Gemini responseSchema(OpenAPI 형식): type을 대문자로 쓰고 속성 순서를 지정한다."""
    if isinstance(node, dict):
        out = {k: (v.upper() if k == "type" else _openapi_schema(v)) for k, v in node.items()
               if k != "additionalProperties"}
        if "properties" in node:
            out["propertyOrdering"] = list(node["properties"])
        return out
    if isinstance(node, list):
        return [_openapi_schema(v) for v in node]
    return node


def ask_gemini(job, api_key, model):
    """Gemini API로 영역마다 팔레트를 고른다."""
    parts = [{"text": "[레퍼런스]"}]
    parts += [{"inline_data": {"mime_type": "image/png", "data": _b64(im)}} for im in job.ref_images]
    parts.append({"text": "[원고 지도]"})
    parts += [{"inline_data": {"mime_type": "image/png", "data": _b64(im)}} for im in job.map_images]
    parts.append({"text": job.prompt})
    url = f"https://generativelanguage.googleapis.com/v1beta/models/{urllib.parse.quote(model)}:generateContent"
    headers = {"x-goog-api-key": api_key}
    body = {"contents": [{"role": "user", "parts": parts}],
            "generationConfig": {"responseMimeType": "application/json",
                                 "responseJsonSchema": _strip_schema(job.schema),
                                 # 기본 해상도로는 이미지를 작게 줄여 봐서 지도 번호를 읽지 못한다
                                 "mediaResolution": "MEDIA_RESOLUTION_HIGH"}}
    try:
        data = _post(url, headers, body)
    except RuntimeError as e:
        if "responseJsonSchema" not in str(e):
            raise
        # responseJsonSchema를 모르는 모델이면 예전 형식(responseSchema)으로 다시 묻는다
        body["generationConfig"] = {"responseMimeType": "application/json",
                                    "responseSchema": _openapi_schema(job.schema),
                                    "mediaResolution": "MEDIA_RESOLUTION_HIGH"}
        data = _post(url, headers, body)
    cand = (data.get("candidates") or [{}])[0]
    text = "".join(p.get("text", "") for p in cand.get("content", {}).get("parts", []))
    if not text:
        raise RuntimeError(f"AI가 답하지 않았습니다 (finishReason: {cand.get('finishReason')})")
    meta = data.get("usageMetadata", {})
    job.usage = {"input": meta.get("promptTokenCount", 0), "output": meta.get("candidatesTokenCount", 0), "model": model}
    return json.loads(text)


def ask_openai(job, api_key, model):
    """OpenAI Responses API로 영역마다 팔레트를 고른다."""
    content = [{"type": "input_text", "text": "[레퍼런스]"}]
    content += [{"type": "input_image", "image_url": f"data:image/png;base64,{_b64(im)}"} for im in job.ref_images]
    content.append({"type": "input_text", "text": "[원고 지도]"})
    content += [{"type": "input_image", "image_url": f"data:image/png;base64,{_b64(im)}"} for im in job.map_images]
    content.append({"type": "input_text", "text": job.prompt})
    body = {"model": model, "input": [{"role": "user", "content": content}],
            "text": {"format": {"type": "json_schema", "name": "region_palette", "schema": job.schema, "strict": True}}}
    data = _post("https://api.openai.com/v1/responses", {"Authorization": f"Bearer {api_key}"}, body)
    text = "".join(c.get("text", "") for item in data.get("output", []) if item.get("type") == "message"
                   for c in item.get("content", []) if c.get("type") == "output_text")
    if not text:
        raise RuntimeError("AI가 답하지 않았습니다")
    usage = data.get("usage", {})
    job.usage = {"input": usage.get("input_tokens", 0), "output": usage.get("output_tokens", 0), "model": model}
    return json.loads(text)


def ask(job, provider, api_key, model):
    if provider == "claude":
        return ask_claude(job, api_key, model)
    if provider == "gemini":
        return ask_gemini(job, api_key, model)
    if provider == "openai":
        return ask_openai(job, api_key, model)
    raise RuntimeError(f"알 수 없는 AI 제공자: {provider}")


NONE_TINT = (255, 170, 210)  # 검수 지도에서 none 영역 색


def review_job(job, labels, boundary, answer):
    """이전 답대로 칠한 지도를 만들어 검수 요청을 꾸린다."""
    pal = {p["id"]: p["rgb"] for p in job.palette}
    pick = {item["region"]: item["palette"] for item in answer.get("assignments", [])}
    cand_ids = job.candidates[1:]
    colors = np.array([pal.get(pick.get(k), NONE_TINT) for k in range(1, len(cand_ids) + 1)], np.uint8)
    maps, _ = build_map(labels, boundary, cand_ids, colors)
    prompt = REVIEW_PROMPT.format(palette_list=job.palette_text, count=len(cand_ids))
    schema = {k: v for k, v in job.schema.items()}
    schema["properties"] = {k: v for k, v in job.schema["properties"].items() if k != "layer_names"}
    schema["required"] = list(schema["properties"])
    return Job(job.candidates, job.palette, job.ref_images, maps, prompt, schema, palette_text=job.palette_text,
               groups=job.groups, areas=job.areas)


def merge_review(answer, review):
    """검수에서 고친 번호만 덮어쓴다."""
    fixed = {item["region"]: item["palette"] for item in review.get("assignments", [])}
    merged = [{"region": a["region"], "palette": fixed.pop(a["region"], a["palette"])}
              for a in answer.get("assignments", [])]
    merged += [{"region": k, "palette": v} for k, v in fixed.items()]
    return {**answer, "assignments": merged, "review": review.get("analysis", ""), "fixed": len(review.get("assignments", []))}


def save_job(job, folder):
    """Claude Code·Codex 같은 도구에서 답을 받을 수 있게 이미지와 지시문을 폴더로 내보낸다."""
    folder = Path(folder)
    folder.mkdir(parents=True, exist_ok=True)
    names = []
    for i, im in enumerate(job.ref_images, 1):
        names.append(f"ref_{i}.png")
        im.convert("RGB").save(folder / names[-1])
    for i, im in enumerate(job.map_images, 1):
        names.append(f"map_{i}.png")
        im.convert("RGB").save(folder / names[-1])
    (folder / "schema.json").write_text(json.dumps(job.schema, ensure_ascii=False, indent=2))
    (folder / "README.md").write_text(
        "# psd-paint AI 매칭 요청\n\n"
        f"이미지: {', '.join(names)} (ref_* = 레퍼런스, map_* = 원고 지도)\n\n"
        f"{job.prompt}\n\n"
        "답은 schema.json 형식의 JSON으로 이 폴더의 answer.json에 저장한다.\n"
        "저장한 뒤 psd-paint에서 같은 파일을 다시 실행하면 답을 적용한다.\n")
    return folder


def fill_group_gaps(job, answer, share=0.6):
    """묶음 안에서 none으로 남은 조각은, 묶음 넓이의 share 이상을 차지한 팔레트를 따른다."""
    if job.groups is None:
        return answer
    pick = {item["region"]: item["palette"] for item in answer.get("assignments", [])}
    k_all = np.arange(1, len(job.candidates))
    for g in np.unique(job.groups):
        members = k_all[job.groups == g]
        if len(members) < 2:
            continue
        area = {}
        for k in members:
            p = pick.get(int(k), "none")
            if p != "none":
                area[p] = area.get(p, 0) + int(job.areas[k - 1])
        if not area:
            continue
        top, top_area = max(area.items(), key=lambda kv: kv[1])
        if top_area >= share * job.areas[members - 1].sum():
            for k in members:
                if pick.get(int(k), "none") == "none":
                    pick[int(k)] = top
    return {**answer, "assignments": [{"region": k, "palette": p} for k, p in sorted(pick.items())]}


def apply_answer(job, answer, n_regions):
    """답 → (영역별 키 배열, 키 목록[(레이어 이름, RGB)]). 고르지 못한 영역은 -1."""
    names = {p["id"]: p["name"] for p in job.palette}
    for item in answer.get("layer_names", []):
        if not names.get(item["palette"]):
            names[item["palette"]] = item["layer"].strip() or None
    keys, key_of = [], {}
    for p in job.palette:
        rgb = p["rgb"]
        key_of[p["id"]] = len(keys)
        keys.append((names.get(p["id"]) or f"색_{rgb[0]:02X}{rgb[1]:02X}{rgb[2]:02X}", rgb))
    region_key = np.full(n_regions + 1, -1, np.int64)
    for item in answer.get("assignments", []):
        k, pid = item.get("region"), item.get("palette")
        if pid in key_of and isinstance(k, int) and 1 <= k < len(job.candidates):
            region_key[job.candidates[k]] = key_of[pid]
    return region_key, keys
