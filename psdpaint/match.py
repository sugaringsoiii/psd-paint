"""다른 페이지를 칠한 레퍼런스를 보고 원고의 영역마다 부위를 정한다 (AI 매칭).

1. 원고에서 칠할 영역에 번호를 붙인 지도 이미지를 만든다.
2. 레퍼런스에서 색(팔레트)을 뽑고 P1, P2 … 표시를 얹은 이미지를 만든다.
3. AI가 장면을 이해해 인물마다 부위(머리카락·피부·겉옷 …)를 나누고, 인물×부위마다 팔레트를 고른다.
   같은 인물의 같은 부위는 코드가 같은 색으로 칠한다.
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
    map_label: str = "[원고 지도]"  # map_images 앞에 붙이는 이름표 (단계마다 다르다)
    occlusion_text: str = "없음"     # 가림 단서 (좁은 띠를 사이에 둔 번호 쌍)
    full_map: Image.Image = None  # 번호를 얹은 전체 원고 지도
    kmap: np.ndarray = None       # 픽셀 → 지도 번호


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


def build_map(labels, boundary, cand_ids, colors=None, groups=None, hatch=None):
    """원고 지도: 칠할 영역마다 색과 번호. → (이미지 목록, 지도 번호 → 영역 번호 배열)

    colors가 없으면 묶음(groups)마다 옅은 임의의 색을 쓴다. 같은 묶음은 같은 바탕색이 된다.
    colors가 있으면 그 색(번호 순서, 칠한 결과)을 쓴다.
    """
    h, w = labels.shape
    lut = np.zeros(labels.max() + 1, np.int32)
    lut[cand_ids] = np.arange(1, len(cand_ids) + 1)
    kmap = lut[labels]
    if colors is None:
        # 바탕색을 칠하면 AI가 실제 옷 색으로 착각한다(분홍 머리, 민트 스웨터). 원고처럼 흰 바탕에 번호만 얹는다
        colors = np.full((len(cand_ids), 3), 255)
    tint = np.vstack([[255, 255, 255], colors]).astype(np.uint8)
    rgb = tint[kmap]
    if hatch is not None:
        # 가림 띠는 회색 빗금으로 보여 준다 (앞에 있는 사물이라는 표시)
        yy, xx = np.nonzero(hatch)
        on = ((yy + xx) // 4) % 2 == 0
        rgb[yy[on], xx[on]] = (150, 150, 150)
        rgb[yy[~on], xx[~on]] = (225, 225, 225)
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
    build_map.last = (img, kmap)  # 인물별 확대에 쓰는 전체 지도와 번호 맵
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


def occlusion_cues(labels, cand_ids, bg_mask, reach=16, min_area=600):
    """두 조각 사이에 좁고 긴 배경 통로가 끼어 있는 쌍을 찾는다 (의자 등받이·난간·끈 같은 가림).

    바로 맞닿은 이웃은 선 하나만 사이에 있지만, 가려진 이웃은 그 사이에 배경이 파고든 띠가 있다.
    두 조각을 reach만큼 넓혔을 때 겹치는 곳 중 배경이 min_area 이상이면 가림 쌍으로 본다.
    → ([(지도 번호 a, 지도 번호 b)], 띠 마스크)
    """
    h, w = labels.shape
    boxes = ndimage.find_objects(labels)
    kernel = cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (2 * reach + 1, 2 * reach + 1))
    grown = {}
    for k in cand_ids:
        sl = boxes[k - 1]
        y0, y1 = max(sl[0].start - reach, 0), min(sl[0].stop + reach, h)
        x0, x1 = max(sl[1].start - reach, 0), min(sl[1].stop + reach, w)
        grown[k] = (y0, x0, cv2.dilate((labels[y0:y1, x0:x1] == k).astype(np.uint8), kernel) > 0)
    pairs, strip = [], np.zeros((h, w), bool)
    for i, a in enumerate(cand_ids):
        ay, ax, ma = grown[a]
        for j in range(i + 1, len(cand_ids)):
            by, bx, mb = grown[cand_ids[j]]
            y0, x0 = max(ay, by), max(ax, bx)
            y1, x1 = min(ay + ma.shape[0], by + mb.shape[0]), min(ax + ma.shape[1], bx + mb.shape[1])
            if y1 <= y0 or x1 <= x0:
                continue
            between = ma[y0 - ay:y1 - ay, x0 - ax:x1 - ax] & mb[y0 - by:y1 - by, x0 - bx:x1 - bx] & bg_mask[y0:y1, x0:x1]
            if between.sum() >= min_area:
                pairs.append((i + 1, j + 1))
                strip[y0:y1, x0:x1] |= between
    return pairs, strip


def make_job(labels, boundary, cand_ids, ref_rgb, palette, masks, bg_mask=None):
    groups = region_groups(labels, boundary, cand_ids)
    pairs, strip = occlusion_cues(labels, cand_ids, bg_mask) if bg_mask is not None else ([], None)
    occlusion_text = " ".join(f"{a}↔{b}" for a, b in pairs) or "없음"
    map_images, cands = build_map(labels, boundary, cand_ids, groups=groups, hatch=strip)
    ref_images = build_reference(ref_rgb, masks, palette)
    ids = [p["id"] for p in palette]
    palette_list = ", ".join(f"{p['id']}=#{p['rgb'][0]:02X}{p['rgb'][1]:02X}{p['rgb'][2]:02X}"
                             + (f"({p['name'] or p.get('note')})" if p["name"] or p.get("note") else "")
                             for p in palette)
    areas = np.bincount(labels.ravel(), minlength=labels.max() + 1)[cand_ids]
    full_map, kmap = build_map.last
    prompt = FULL_PROMPT.format(parts=", ".join(PARTS), palette_list=palette_list, count=len(cand_ids),
                                groups=group_text(groups=groups), occlusion=occlusion_text)
    schema = full_schema(ids)
    return Job(cands, palette, ref_images, map_images, prompt, schema, palette_text=palette_list,
               groups=groups, areas=areas, occlusion_text=occlusion_text, full_map=full_map, kmap=kmap)


def _b64(img):
    buf = io.BytesIO()
    img.convert("RGB").save(buf, "PNG", optimize=True)
    return base64.standard_b64encode(buf.getvalue()).decode()


def ask_claude(job, api_key, model):
    """Claude API로 영역마다 팔레트를 고른다. → 답 dict. 토큰 사용량은 job.usage에 남긴다."""
    import anthropic

    content = [{"type": "text", "text": "[레퍼런스]"}] if job.ref_images else []
    content += [{"type": "image", "source": {"type": "base64", "media_type": "image/png", "data": _b64(im)}}
                for im in job.ref_images]
    content.append({"type": "text", "text": job.map_label})
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
    parts = [{"text": "[레퍼런스]"}] if job.ref_images else []
    parts += [{"inline_data": {"mime_type": "image/png", "data": _b64(im)}} for im in job.ref_images]
    parts.append({"text": job.map_label})
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
    content = [{"type": "input_text", "text": "[레퍼런스]"}] if job.ref_images else []
    content += [{"type": "input_image", "image_url": f"data:image/png;base64,{_b64(im)}"} for im in job.ref_images]
    content.append({"type": "input_text", "text": job.map_label})
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
    ids = [p["id"] for p in job.palette]
    schema = _obj({"analysis": {"type": "string"}, "assignments": {"type": "array", "items": _obj(
        {"region": {"type": "integer"}, "palette": {"type": "string", "enum": ids + ["none"]}})}})
    return Job(job.candidates, job.palette, job.ref_images, maps, prompt, schema, palette_text=job.palette_text,
               groups=job.groups, areas=job.areas)


def merge_review(answer, review):
    """검수에서 고친 번호만 덮어쓴다."""
    fixed = {item["region"]: item["palette"] for item in review.get("assignments", [])}
    # 고친 번호의 레이어는 같은 팔레트를 쓰는 다른 번호의 레이어를 따른다
    layer_of = {a["palette"]: a.get("layer") for a in answer.get("assignments", []) if a.get("layer")}
    merged = []
    for a in answer.get("assignments", []):
        if a["region"] in fixed:
            p = fixed.pop(a["region"])
            merged.append({"region": a["region"], "palette": p, "layer": layer_of.get(p)})
        else:
            merged.append(a)
    merged += [{"region": k, "palette": v, "layer": layer_of.get(v)} for k, v in fixed.items()]
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
    layer = {item["region"]: item.get("layer") for item in answer.get("assignments", [])}
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
            top_layer = next((layer.get(int(k)) for k in members if pick.get(int(k)) == top), None)
            for k in members:
                if pick.get(int(k), "none") == "none":
                    pick[int(k)], layer[int(k)] = top, top_layer
    return {**answer, "assignments": [{"region": k, "palette": p, "layer": layer.get(k)} for k, p in sorted(pick.items())]}


def apply_answer(job, answer, n_regions):
    """답 → (영역별 키 배열, 키 목록[(레이어 이름, RGB)]). 고르지 못한 영역은 -1."""
    names = {p["id"]: p["name"] for p in job.palette}
    for item in answer.get("layer_names", []):
        if not names.get(item["palette"]):
            names[item["palette"]] = item["layer"].strip() or None
    rgb_of = {p["id"]: p["rgb"] for p in job.palette}
    keys, key_of = [], {}
    region_key = np.full(n_regions + 1, -1, np.int64)
    for item in answer.get("assignments", []):
        k, pid = item.get("region"), item.get("palette")
        if pid not in rgb_of or not isinstance(k, int) or not 1 <= k < len(job.candidates):
            continue
        rgb = rgb_of[pid]
        # 레이어 이름: 답에 적힌 부위 레이어 → 레퍼런스 레이어 이름 → 색 코드
        layer = item.get("layer") or names.get(pid) or f"색_{rgb[0]:02X}{rgb[1]:02X}{rgb[2]:02X}"
        if (layer, pid) not in key_of:
            key_of[(layer, pid)] = len(keys)
            keys.append((layer, rgb))
        region_key[job.candidates[k]] = key_of[(layer, pid)]
    return region_key, keys



# ---------------------------------------------------------------------------
# 구조화 매칭: 장면 이해 → 인물×부위 색 → 번호별 색
#
# 번호마다 바로 색을 고르면 작은 조각에서 인물·부위를 헷갈린다. 먼저 인물마다 자세와 부위(머리카락·피부·겉옷 …)를
# 나누게 하고, 색은 인물×부위마다 한 번만 고르게 한다. 같은 인물의 같은 부위는 코드가 같은 색으로 칠한다.
# ---------------------------------------------------------------------------

PARTS = ["머리카락", "피부", "겉옷", "상의", "셔츠", "넥타이·리본", "하의", "치마", "신발", "양말",
         "머리띠·모자", "장신구·소품", "기타"]
LAYER_OF_PART = {"머리카락": "머리", "넥타이·리본": "넥타이", "머리띠·모자": "머리띠", "장신구·소품": "소품"}
# 레퍼런스 색이 없는 부위(엑스트라의 옷 등)에 쓰는 회색. 같은 인물의 같은 부위는 조각이 나뉘어도 한 회색으로 칠한다
PART_TONES = {"머리카락": 105, "겉옷": 150, "상의": 185, "셔츠": 235, "넥타이·리본": 110, "하의": 125, "치마": 125,
              "신발": 80, "양말": 95, "머리띠·모자": 140, "장신구·소품": 160, "기타": 200}

SCENE_RULES = """장면을 이해하는 순서
1. 인물을 하나씩 찾는다. 인물마다 자세(서 있음·앉음·고개 숙임·팔 위치 등)와 겉모습(얼굴·머리모양·머리띠·옷차림)을 적는다.
   같은 인물이 여러 컷에 나오면(전신 컷과 클로즈업 등) 하나의 id에 모은다. 단, 머리모양·머리띠·얼굴이 다르면 다른 인물이다.
   서로 다른 두 사람을 한 id로 합치지 않는다. 손·팔만 보여도 그 컷의 인물에 붙인다.
2. 자세를 바탕으로 인체 구조를 따라간다. 머리 → 얼굴·목 → 몸통 → 양팔(소매와 손) → 다리(하의와 신발) 순서로, 가려진 부분이 어디로 이어지는지 생각한다.
3. 옷은 겹침 순서로 나눈다. 겉옷(재킷·가디건·코트) / 상의(스웨터·니트·티셔츠·조끼) / 셔츠(깃·소매 끝) / 넥타이·리본 / 하의 / 치마 / 신발 / 양말.
   팔 전체가 겉옷 소매면 소매 조각은 모두 겉옷이다. 소매 끝으로 보이는 셔츠 조각만 셔츠다.
4. 주름선이나 끊긴 선으로 나뉜 조각은 같은 옷이다. 의자 등받이·책상·다른 사람 팔처럼 앞에 있는 사물이 몸을 가로질러
   나눈 조각도 같은 옷이다 (가려진 너머로 옷이 이어진다).
   아래 '가림 단서'는 두 조각 사이에 좁고 긴 띠(지도에 회색 빗금)가 끼어 있는 쌍이다. 띠는 앞에 있는 사물이고 몸은 그 뒤로 이어진다.
   띠 양쪽에서 옷의 바깥선(실루엣)이 이어지고 몸의 높이가 맞으면 같은 부위로 묶는다
   (등을 가로지르는 띠 아래는 아직 상의다. 하의는 엉덩이·다리부터다). 실루엣이 이어지지 않으면 따로 둔다. 아래 '이어진 묶음'의 번호들은 선이 끊긴 틈으로 이어진 한 덩어리다.
   지도는 흰 바탕의 선화에 번호만 얹은 것이다. 색은 그림에 없으므로 옷 색을 짐작해 적지 않는다.
5. 피부는 얼굴·목·귀·손·팔·다리처럼 맨살이 보이는 곳이다. 머리카락과 얼굴 경계를 특히 조심한다(앞머리 사이로 보이는 이마는 피부).
6. 의자·컵·폰 같은 사물은 objects에, 하늘·벽·바닥 같은 배경은 background에 넣는다.
7. 지도의 모든 번호를 빠짐없이 정확히 한 곳(인물의 부위, objects, background)에 넣는다."""

COLOR_RULES = """색을 정하는 순서
1. 레퍼런스 인물마다 알아볼 특징과 부위별 팔레트를 정리한다.
2. 원고 인물마다 같은 레퍼런스 인물을 찾는다. 얼굴·머리모양·머리띠로 알아보고 옷차림은 참고만 한다(옷은 회차마다 바뀐다).
   찾으면 머리카락·피부·머리띠는 그 인물의 팔레트를 쓴다. 옷도 같은 종류(교복 재킷·셔츠·넥타이 등)면 그 팔레트를 쓰고,
   확실히 다른 옷(레퍼런스에 없는 스웨터 등)일 때만 none으로 둔다.
3. 레퍼런스에 없는 인물(엑스트라)은 피부만 피부 팔레트를 고르고, 나머지 부위는 none으로 둔다.
4. 피부는 엑스트라도 반드시 피부 팔레트다."""

FULL_PROMPT = """웹툰 밑색 작업이다. 이미지 두 묶음을 준다.

[레퍼런스] 같은 작품의 다른 페이지를 칠해 둔 그림이다. 칠한 색마다 P1, P2 … 표시를 얹었다.
[원고 지도] 지금 칠할 페이지의 선화다. 칠할 영역마다 옅은 바탕색과 숫자를 얹었다.

""" + SCENE_RULES + """

""" + COLOR_RULES + """

답 형식
- analysis: 레퍼런스 인물 정리와 원고 인물 대응을 짧게 적는다.
- people: 원고 인물마다 id, description(겉모습), pose(자세), reference_character(대응하는 레퍼런스 인물 또는 "없음"),
  parts(부위마다 part, regions(번호 목록), palette(P번호 또는 none)).
- objects: 사물 이름과 번호 목록. background: 배경 번호 목록.
부위 이름: {parts}
이어진 묶음: {groups}
가림 단서: {occlusion}
팔레트: {palette_list}
영역 번호: 1 ~ {count}"""

SCENE_PROMPT = """웹툰 밑색 작업의 첫 단계다. [원고 지도]는 칠할 페이지의 선화이고, 칠할 영역마다 옅은 바탕색과 숫자를 얹었다.
색은 아직 고르지 않는다. 장면의 인물과 부위만 나눈다.

""" + SCENE_RULES + """

답 형식: analysis(장면 요약), people(id, description, pose, parts[part, regions]), objects(name, regions), background(번호 목록)
부위 이름: {parts}
이어진 묶음: {groups}
가림 단서: {occlusion}
영역 번호: 1 ~ {count}"""

ZOOM_PROMPT = """웹툰 밑색 작업의 확대 검토다. [인물 확대]는 인물마다 원고 지도를 크게 잘라 낸 그림이다(번호가 같다).
아래는 앞 단계에서 나눈 인물별 부위다. 확대 그림을 보고 틀린 부분을 고쳐 인물마다 parts를 다시 적는다.
- 자세와 인체 구조를 따라 부위가 맞는지 확인한다 (머리카락↔얼굴, 소매↔손, 겉옷↔안쪽 옷).
- 그 인물이 아닌 번호(다른 인물·사물·배경)가 섞였으면 빼고, 빠진 번호가 있으면 넣는다.
- 확대 그림에 없는 인물은 그대로 둔다.

""" + SCENE_RULES + """

앞 단계 결과:
{scene}
가림 단서: {occlusion}
부위 이름: {parts}"""

COLOR_PROMPT = """웹툰 밑색 작업의 색 정하기 단계다.
[레퍼런스]는 같은 작품의 다른 페이지를 칠해 둔 그림이고, 칠한 색마다 P1, P2 … 표시를 얹었다.
[원고 인물]은 지금 칠할 페이지의 인물을 하나씩 잘라 낸 그림이다(순서대로 {ids}).

""" + COLOR_RULES + """

원고 인물과 부위:
{people}
팔레트: {palette_list}
답 형식: analysis, people(id, reference_character, colors[part, palette])"""


def _int_list():
    return {"type": "array", "items": {"type": "integer"}}


def _obj(props):
    return {"type": "object", "properties": props, "required": list(props), "additionalProperties": False}


def scene_schema(with_palette_ids=None):
    part = {"part": {"type": "string", "enum": PARTS}, "regions": _int_list()}
    if with_palette_ids is not None:
        part["palette"] = {"type": "string", "enum": with_palette_ids + ["none"]}
    person = {"id": {"type": "string"}, "description": {"type": "string"}, "pose": {"type": "string"}}
    if with_palette_ids is not None:
        person["reference_character"] = {"type": "string"}
    person["parts"] = {"type": "array", "items": _obj(part)}
    return _obj({
        "analysis": {"type": "string"},
        "people": {"type": "array", "items": _obj(person)},
        "objects": {"type": "array", "items": _obj({"name": {"type": "string"}, "regions": _int_list()})},
        "background": _int_list(),
    })


def full_schema(palette_ids):
    """한 번에 답하는 형식 (요청 폴더·MCP): 장면 이해와 부위별 팔레트를 함께 적는다."""
    return scene_schema(palette_ids)


def color_schema(palette_ids):
    color = _obj({"part": {"type": "string", "enum": PARTS},
                  "palette": {"type": "string", "enum": palette_ids + ["none"]}})
    person = _obj({"id": {"type": "string"}, "reference_character": {"type": "string"},
                   "colors": {"type": "array", "items": color}})
    return _obj({"analysis": {"type": "string"}, "people": {"type": "array", "items": person}})


def _scene_text(scene):
    lines = []
    for p in scene.get("people", []):
        parts = "; ".join(f"{x['part']}: {', '.join(map(str, x['regions']))}" for x in p.get("parts", []))
        lines.append(f"- 인물 {p['id']} ({p.get('description', '')}, {p.get('pose', '')}): {parts}")
    for o in scene.get("objects", []):
        lines.append(f"- 사물 {o['name']}: {', '.join(map(str, o['regions']))}")
    if scene.get("background"):
        lines.append(f"- 배경: {', '.join(map(str, scene['background']))}")
    return "\n".join(lines)


def group_text(job=None, groups=None):
    """이어진 묶음(번호 2개 이상)을 글로 적는다. 예: [29, 30, 31] [40, 45]"""
    groups = job.groups if groups is None else groups
    if groups is None:
        return "없음"
    out = []
    for g in np.unique(groups):
        ks = np.nonzero(groups == g)[0] + 1
        if len(ks) > 1:
            out.append("[" + ", ".join(map(str, ks)) + "]")
    return " ".join(out) or "없음"


def person_crops(job, scene, max_side=1000):
    """인물마다 원고 지도를 잘라 크게 만든다. → [(인물 id, 이미지)]"""
    out = []
    for p in scene.get("people", []):
        ks = [k for part in p.get("parts", []) for k in part.get("regions", [])]
        m = np.isin(job.kmap, ks) & (job.kmap > 0)
        if not m.any():
            continue
        ys, xs = np.nonzero(m)
        pad = 60
        box = (max(0, xs.min() - pad), max(0, ys.min() - pad),
               min(job.kmap.shape[1], xs.max() + pad), min(job.kmap.shape[0], ys.max() + pad))
        im = job.full_map.crop(box)
        scale = min(3.0, max_side / max(im.size))
        if scale > 1.05 or scale < 0.95:
            im = im.resize((round(im.width * scale), round(im.height * scale)), Image.LANCZOS)
        out.append((p["id"], im))
    return out


def _sub(job, ref_images, map_images, prompt, schema, map_label="[원고 지도]"):
    return Job(job.candidates, job.palette, ref_images, map_images, prompt, schema, palette_text=job.palette_text,
               groups=job.groups, areas=job.areas, map_label=map_label, occlusion_text=job.occlusion_text,
               full_map=job.full_map, kmap=job.kmap)


def ask_structured(job, provider, api_key, model, zoom=True, say=None):
    """API로 장면 이해 → (인물별 확대 보정) → 색 정하기를 차례로 묻는다. → 한 번에 답하는 형식의 답"""
    say = say or (lambda msg: None)
    total = {"input": 0, "output": 0, "calls": 0, "model": model}

    def run(sub):
        result = ask(sub, provider, api_key, model)
        for key in ("input", "output"):
            total[key] += sub.usage.get(key, 0)
        total["model"] = sub.usage.get("model", model)
        total["calls"] += 1
        return result

    count = len(job.candidates) - 1
    say("AI 장면 이해 중")
    scene = run(_sub(job, [], job.map_images, SCENE_PROMPT.format(parts=", ".join(PARTS), count=count,
                                                                   groups=group_text(job),
                                                                   occlusion=job.occlusion_text),
                     scene_schema()))
    crops = person_crops(job, scene)
    if zoom and crops:
        say("AI 인물별 확대 검토 중")
        z = run(_sub(job, [], [im for _, im in crops],
                     ZOOM_PROMPT.format(scene=_scene_text(scene), parts=", ".join(PARTS),
                                        occlusion=job.occlusion_text), scene_schema(),
                     "[인물 확대]"))
        fixed = {p["id"]: p for p in z.get("people", [])}
        scene["people"] = [fixed.get(p["id"], p) for p in scene.get("people", [])]
        crops = person_crops(job, scene)
    say("AI 색 정하는 중")
    ids = [p["id"] for p in job.palette]
    colors = run(_sub(job, job.ref_images, [im for _, im in crops],
                      COLOR_PROMPT.format(ids=", ".join(i for i, _ in crops), people=_scene_text(scene),
                                          palette_list=job.palette_text), color_schema(ids), "[원고 인물]"))
    pick = {(c["id"], x["part"]): x["palette"] for c in colors.get("people", []) for x in c.get("colors", [])}
    ref_of = {c["id"]: c.get("reference_character", "") for c in colors.get("people", [])}
    for p in scene.get("people", []):
        p["reference_character"] = ref_of.get(p["id"], "")
        for part in p.get("parts", []):
            part["palette"] = pick.get((p["id"], part["part"]), "none")
    scene["analysis"] = f"{scene.get('analysis', '')}\n\n[색] {colors.get('analysis', '')}".strip()
    job.usage = total
    return scene


def structured_to_assignments(job, answer):
    """한 번에 답하는 형식 → 번호별 (팔레트, 레이어 이름). 이미 assignments 형식이면 그대로 쓴다."""
    if "people" not in answer:
        return answer
    skin = next((p["id"] for p in job.palette if p.get("name") == "피부"), None) or \
        next((p["id"] for p in job.palette if min(p["rgb"]) >= 248), None)
    name_of = {p["id"]: p.get("name") for p in job.palette}
    out = {}
    for n, person in enumerate(answer.get("people", [])):
        for part in person.get("parts", []):
            pid = part.get("palette", "none")
            if part["part"] == "피부" and pid == "none" and skin:
                pid = skin
            if pid == "none" and part.get("regions"):
                # 레퍼런스에 없는 부위: 인물×부위마다 회색 하나를 만들어 조각이 나뉘어도 같은 색으로 칠한다.
                # 옆 사람과 섞이지 않게 인물마다 조금씩 밝기를 바꾼다
                tone = int(np.clip(PART_TONES.get(part["part"], 200) + (n % 3 - 1) * 12, 40, 245))
                pid = f"auto:{person['id']}:{part['part']}"
                if pid not in name_of:
                    job.palette.append({"id": pid, "rgb": (tone, tone, tone),
                                        "name": LAYER_OF_PART.get(part["part"], part["part"])})
                    name_of[pid] = job.palette[-1]["name"]
            layer = name_of.get(pid) or LAYER_OF_PART.get(part["part"], part["part"])
            for k in part.get("regions", []):
                out[int(k)] = {"region": int(k), "palette": pid, "layer": layer}
    # 결과를 보고 고친 번호 (MCP·요청 폴더에서 덧붙인 수정)
    layer_of = {v["palette"]: v["layer"] for v in out.values()}
    for c in answer.get("corrections", []):
        k, pid = int(c["region"]), c["palette"]
        out[k] = {"region": k, "palette": pid, "layer": name_of.get(pid) or layer_of.get(pid)}
    return {**answer, "assignments": [out[k] for k in sorted(out)]}
