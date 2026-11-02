"""캐릭터 프리셋으로 칠하기.

1. 이미지 모델(Gemini 나노 바나나)이 컷마다 선화를 두 번 칠한다: 부위별 코드 색, 사람별 코드 색.
   이미지 모델은 위치를 그림으로 바로 내놓으므로 '이 칸이 머리카락'을 번호로 가리키는 것보다 정확하다.
2. 우리 영역마다 칠해진 색을 다수결해 부위와 사람을 정한다.
3. 글 AI가 사람마다 잘라 낸 선화를 보고 등록한 캐릭터 중 누구인지 고른다 (API, 또는 요청 폴더·MCP의 AI).
4. 색은 캐릭터 프리셋(색상 프리셋 '캐릭터/부위')에서 가져온다. AI가 톤을 고르지 않으므로 흔들리지 않는다.
"""
import base64
import io
import json
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import numpy as np
from PIL import Image
from scipy import ndimage

from . import match

IMAGE_MODEL = "gemini-3.1-flash-lite-image"
IMAGE_MODELS = ["gemini-3.1-flash-lite-image", "gemini-3.1-flash-image", "gemini-3-pro-image"]

# 프리셋 이름 '캐릭터/부위'의 부위 말 → 부위
PART_ALIASES = {
    "머리카락": ["머리카락", "머리", "헤어", "머리색"],
    "피부": ["피부", "살", "살색"],
    "머리띠·모자": ["머리띠", "모자", "헤어밴드", "머리띠·모자"],
    "겉옷": ["겉옷", "재킷", "자켓", "코트", "블레이저", "가디건", "점퍼"],
    "상의": ["상의", "조끼", "니트", "스웨터", "티셔츠", "후드", "맨투맨"],
    "셔츠": ["셔츠", "블라우스"],
    "넥타이·리본": ["넥타이", "리본", "타이", "넥타이·리본"],
    "하의": ["하의", "바지", "슬랙스"],
    "치마": ["치마", "스커트"],
    "신발": ["신발", "구두", "운동화"],
    "양말": ["양말"],
    "장신구·소품": ["소품", "장신구", "귀걸이", "안경", "장신구·소품"],
}
PART_OF_WORD = {w: part for part, words in PART_ALIASES.items() for w in words}

PART_CODES = {"머리카락": (255, 0, 0), "피부": (255, 255, 0), "머리띠·모자": (255, 0, 255), "겉옷": (0, 0, 255),
              "상의": (0, 255, 255), "셔츠": (0, 255, 0), "넥타이·리본": (128, 0, 255), "하의": (139, 69, 19),
              "신발": (64, 64, 64), "사물": (255, 128, 0), "배경": (255, 255, 255)}
PART_NAMES = list(PART_CODES)
BODY = [p for p in PART_NAMES if p not in ("사물", "배경")]
PERSON_CODES = {"A": (255, 0, 0), "B": (0, 0, 255), "C": (0, 200, 0), "D": (255, 160, 0), "배경": (255, 255, 255)}
KEEP = ("Keep every black line exactly where it is; do not redraw, move, add or remove lines, and never draw new hands, "
        "people or objects. Do not change the composition or size. Flat solid fills only (no shading, gradients or "
        "texture). Output only the image.")
PARTS_PROMPT = """This is one panel of black-and-white manga line art. Make a flat color-coded part map of it.
Fill each closed area between the lines with ONE color by what it is:
hair #FF0000, skin (face, forehead, ears, neck, hands, fingers, arms) #FFFF00, headband/hat #FF00FF,
jacket/blazer/coat incl. lapels and sleeves #0000FF, sweater/vest/knit top #00FFFF, shirt (collar, cuffs, shirt front) #00FF00,
necktie/ribbon #8000FF, pants/skirt #8B4513, shoes #404040, objects (cups, phones, bags, furniture, props) #FF8000,
background: pure white.
Rules:
- A hand holding an object: the hand and fingers are skin, only the object is orange.
- Every area inside a person's silhouette gets a body color: small gaps between hair strands are hair, gaps between fingers that show the face are skin.
- Background seen between body parts (outside the silhouette) stays white.
- Parts of the same garment separated by folds or by something in front get the same color.
""" + KEEP
PERSONS_PROMPT = """This is one panel of black-and-white manga line art. Make a flat color map of the people in it.
Give each distinct person one color, from left to right: first person #FF0000, second #0000FF, third #00C800, fourth #FFA000.
Fill the person's whole silhouette with that color: hair, face, skin, every piece of clothing and accessories,
and a hand or arm of that person reaching in from the panel edge. Do not leave gaps inside the silhouette.
Objects that nobody wears and the background stay pure white.
""" + KEEP
RATIOS = {"1:1": 1, "2:3": 2 / 3, "3:2": 3 / 2, "3:4": 3 / 4, "4:3": 4 / 3, "4:5": 4 / 5, "5:4": 5 / 4,
          "9:16": 9 / 16, "16:9": 16 / 9, "21:9": 21 / 9}

IDENTIFY_PROMPT = """웹툰 밑색 작업이다. [원고 인물]은 이번 페이지의 인물을 하나씩 잘라 낸 선화다. 순서대로 인물 {ids}이다.
인물마다 아래 등장인물 중 누구인지 얼굴·머리 모양·머리띠·귀걸이로 알아본다.
선화라서 머리 색은 선의 밀도(짙은 머리는 결이 촘촘하거나 칠해짐)로 짐작한다. 옷차림은 회차마다 바뀌므로 참고만 한다.
목록에 없는 인물(엑스트라)이거나 판단이 서지 않으면 모름이라고 적는다.
등장인물:
{looks}"""


def from_presets(presets, looks=None):
    """색상 프리셋 [{"name": "하늘/머리", "rgb"}] + 생김새 {"하늘": "…"} → 캐릭터 [{"name", "look", "colors": {부위: rgb}}]"""
    chars = {}
    for p in presets:
        name = p.get("name") or ""
        if "/" not in name:
            continue
        who, word = (s.strip() for s in name.split("/", 1))
        part = PART_OF_WORD.get(word)
        if who and part:
            chars.setdefault(who, {"name": who, "look": "", "colors": {}})["colors"][part] = tuple(p["rgb"])
    for who, look in (looks or {}).items():
        if who.strip():
            chars.setdefault(who.strip(), {"name": who.strip(), "look": "", "colors": {}})["look"] = look.strip()
    return list(chars.values())


def parse_looks(text):
    """'하늘: 밝은 갈색 곱슬머리, 고양이 귀 머리띠' 줄들 → {"하늘": "…"}"""
    out = {}
    for line in (text or "").splitlines():
        if ":" in line:
            who, look = line.split(":", 1)
            if who.strip():
                out[who.strip()] = look.strip()
    return out


def from_app_settings():
    """앱(psd-paint)에 저장한 색상 프리셋과 캐릭터 생김새를 읽는다 (MCP 서버에서 쓴다)."""
    from PySide6.QtCore import QSettings

    s = QSettings("psd-paint", "psd-paint")
    try:
        presets = json.loads(s.value("presets", "[]"))
    except (ValueError, TypeError):
        presets = []
    return from_presets(presets, parse_looks(s.value("character_looks", "")))


def page_chunks(content, gap=16):
    """컷 단위 조각: 내용(칠할 영역·컷 테두리)이 있는 줄이 gap 줄 이상 비어 끊기는 곳에서 나눈다.

    컷을 중간에서 자르면 이미지 모델이 잘린 손을 지어내 그리므로 길어도 자르지 않는다 (보낼 때 줄어든다).
    → [(y0, y1, x0, x1)]
    """
    h, w = content.shape
    rows = content.any(1)
    out, y = [], 0
    while y < h:
        if not rows[y]:
            y += 1
            continue
        y0, empty = y, 0
        while y < h and empty < gap:
            empty = 0 if rows[y] else empty + 1
            y += 1
        y1 = y - empty
        cols = np.nonzero(content[y0:y1].any(0))[0]
        if cols.size and y1 - y0 >= 40:
            out.append((y0, y1, max(0, int(cols.min()) - match.MARGIN), min(w, int(cols.max()) + 1 + match.MARGIN)))
    return out


def _b64(im):
    buf = io.BytesIO()
    im.convert("RGB").save(buf, "PNG")
    return base64.b64encode(buf.getvalue()).decode()


def generate(lines, prompt, api_key, model):
    """선화 조각을 이미지 모델로 칠한다. → 원래 크기 RGB 배열, 사용량"""
    w, h = lines.size
    ratio = min(RATIOS, key=lambda r: abs(RATIOS[r] - w / h))
    r = RATIOS[ratio]
    # 지원하는 비율로 흰 여백을 붙여 보낸다 (늘이면 선이 어긋난다)
    w2, h2 = (w, round(w / r)) if w / h > r else (round(h * r), h)
    w2, h2 = max(w, w2), max(h, h2)
    canvas = Image.new("RGB", (w2, h2), "white")
    canvas.paste(lines, (0, 0))
    body = {"contents": [{"role": "user", "parts": [{"inline_data": {"mime_type": "image/png", "data": _b64(canvas)}},
                                                     {"text": prompt}]}],
            "generationConfig": {"responseModalities": ["IMAGE"], "imageConfig": {"aspectRatio": ratio}}}
    url = f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"
    data = match._post(url, {"x-goog-api-key": api_key}, body)
    meta = data.get("usageMetadata", {})
    usage = {"input": meta.get("promptTokenCount", 0), "output": meta.get("candidatesTokenCount", 0)}
    cand = (data.get("candidates") or [{}])[0]
    for part in cand.get("content", {}).get("parts", []):
        blob = part.get("inlineData") or part.get("inline_data")
        if blob:
            im = Image.open(io.BytesIO(base64.b64decode(blob["data"]))).convert("RGB")
            return np.asarray(im.resize((w2, h2), Image.NEAREST).crop((0, 0, w, h))), usage
    raise RuntimeError(f"이미지 모델이 그림을 돌려주지 않았습니다 (finishReason: {cand.get('finishReason')})")


def _codes(img, codes):
    """칠한 그림의 픽셀마다 가장 가까운 코드 번호. 선(검정)은 -1."""
    cols = np.array(list(codes.values()), int)
    img = img.astype(int)
    lab = ((img[:, :, None, :] - cols[None, None]) ** 2).sum(3).argmin(2)
    lab[img.sum(2) < 200] = -1
    return lab


def image_maps(labels, boundary, content, api_key, model, folder, say=lambda msg: None):
    """컷마다 부위 지도 2장·사람 지도 1장을 만들고(또는 저장해 둔 것을 읽고) 페이지 크기 픽셀 지도로 모은다.

    content: 컷을 나눌 때 볼 내용 (배경 아닌 영역 + 컷 테두리). 페이지 바깥 여백은 넣지 않는다.
    → part_px (부위 번호, 모르면 -1), person_px (사람 번호, 없으면 -1), 사람 id 목록, 컷 목록, 사용량
    """
    folder = Path(folder)
    folder.mkdir(parents=True, exist_ok=True)
    lines = np.full(labels.shape + (3,), 255, np.uint8)
    lines[boundary] = 0
    chunks = page_chunks(content)
    usage = {"input": 0, "output": 0, "calls": 0, "model": model}
    jobs = []
    for i, (y0, y1, x0, x1) in enumerate(chunks, 1):
        im = Image.fromarray(lines[y0:y1, x0:x1])
        jobs += [(folder / f"parts_{i}.png", im, PARTS_PROMPT), (folder / f"parts_{i}b.png", im, PARTS_PROMPT),
                 (folder / f"persons_{i}.png", im, PERSONS_PROMPT)]
    todo = [j for j in jobs if not j[0].exists()]
    if todo:
        if not api_key:
            raise RuntimeError("캐릭터 프리셋 방식은 부위 나누기에 Gemini API 키가 필요합니다 (설정 → AI 연결).")
        say(f"이미지 모델로 부위·사람 나누는 중 ({len(todo)}장)")

        def run(job):
            path, im, prompt = job
            arr, u = generate(im, prompt, api_key, model)
            Image.fromarray(arr).save(path)
            return u

        with ThreadPoolExecutor(4) as ex:
            for u in ex.map(run, todo):
                usage["input"] += u["input"]
                usage["output"] += u["output"]
                usage["calls"] += 1
    h, w = labels.shape
    part_px = np.full((h, w), -1, np.int16)
    person_px = np.full((h, w), -1, np.int16)
    person_ids = []
    for i, (y0, y1, x0, x1) in enumerate(chunks, 1):
        load = lambda name: np.asarray(Image.open(folder / name).convert("RGB"))
        a, b = _codes(load(f"parts_{i}.png"), PART_CODES), _codes(load(f"parts_{i}b.png"), PART_CODES)
        # 두 장이 같은 픽셀만 믿는다 (돌릴 때마다 흔들리는 곳은 모름으로 두고 둘레를 따른다)
        part_px[y0:y1, x0:x1] = np.where(a == b, a, -1)
        q = _codes(load(f"persons_{i}.png"), PERSON_CODES)
        names = list(PERSON_CODES)
        local = np.full(q.shape, -1, np.int16)
        for j, letter in enumerate(names[:-1]):
            if (q == j).sum() >= 200:
                person_ids.append(f"{i}{letter}")
                local[q == j] = len(person_ids) - 1
        person_px[y0:y1, x0:x1] = local
    # 이미지 모델이 컷 테두리·배경에 사람 색을 칠하기도 한다. 몸(머리카락·피부·옷)이 거의 없는 '사람'은 버린다.
    # 손·팔만 나온 사람도 있으므로 머리는 따지지 않는다
    body = np.isin(part_px, [PART_NAMES.index(p) for p in BODY])
    for j in range(len(person_ids)):
        m = person_px == j
        if (body & m).sum() < max(800, 0.4 * m.sum()):
            person_px[m] = -1
    # 실루엣 안인데 부위 지도 두 장이 모두 배경이라고 한 곳(머리카락 끝 가닥 사이 틈 등)은 가장 가까운 몸 부위로 채운다.
    # 두 장이 엇갈린 곳(모름)은 그대로 두어 영역 다수결에 맡긴다.
    # 사람 지도가 선 밖으로 살짝 번진 곳까지 칠하지 않게 실루엣은 2px 줄여서 쓴다
    body = np.isin(part_px, [PART_NAMES.index(p) for p in BODY])
    inside = ndimage.binary_erosion(person_px >= 0, iterations=2)
    fix = inside & (part_px == PART_NAMES.index("배경"))
    if fix.any() and body.any():
        iy, ix = ndimage.distance_transform_edt(~body, return_indices=True)[1]
        part_px[fix] = part_px[iy[fix], ix[fix]]
    return part_px, person_px, person_ids, chunks, usage


def _counts(labels, px, k, n):
    """영역마다 값별 픽셀 수. → (n+1, k)"""
    ok = (px >= 0) & (labels > 0)
    out = np.zeros((n + 1, k), np.int64)
    np.add.at(out, (labels[ok], px[ok]), 1)
    return out


def split_mixed(labels, part_px, cand, min_px=300, share=0.15):
    """선이 끊겨 한 영역에 부위 둘(배경과 몸, 머리카락과 피부 …)이 섞이면 생성 지도의 경계대로 나눈다.

    labels를 고쳐 새 영역 번호를 붙인다. → 새 영역 수
    """
    n = int(labels.max())
    cnt = _counts(labels, part_px, len(PART_NAMES), n)
    boxes = ndimage.find_objects(labels)
    for r in cand:
        c = cnt[r]
        tot = c.sum()
        if tot < 2 * min_px:
            continue
        top, second = np.argsort(-c)[:2]
        # 배경이 대부분인 영역(컷 배경이 가닥 사이 틈으로 들어온 곳)은 몸 조각이 작아도 떼어 낸다
        need = min_px if PART_NAMES[top] == "배경" else max(min_px, share * tot)
        if c[second] < need:
            continue
        sl = boxes[r - 1]
        m = labels[sl] == r
        lab = np.where(m, part_px[sl], -1)
        known = lab >= 0
        # 모르는 픽셀은 가장 가까운 아는 픽셀의 부위를 따른다
        idx = ndimage.distance_transform_edt(~known, return_indices=True)[1]
        full = lab[idx[0], idx[1]]
        for v in np.unique(full[m]):
            if v == top or c[v] < need:
                continue
            comp, k = ndimage.label(m & (full == v))
            for j in range(1, k + 1):
                piece = comp == j
                if piece.sum() >= min_px // 2:
                    n += 1
                    labels[sl][piece] = n
    return n


def decide(labels, n, part_px, person_px, person_ids, bg_hint):
    """영역마다 부위·사람을 정한다.

    - 사람 실루엣 안(사람 지도에 칠해진 픽셀이 절반 이상)은 빈틈없이 칠한다. 부위를 모르면 둘레를 따른다.
    - 실루엣 밖이고 지도가 배경이라고 한 영역만 비운다.
    → part_of {영역: 부위}, person_of {영역: 사람 id}, background (n+1 bool)
    """
    pc = _counts(labels, part_px, len(PART_NAMES), n)
    qc = _counts(labels, person_px, max(1, len(person_ids)), n)
    areas = np.bincount(labels.ravel(), minlength=n + 1)
    seen = np.bincount(labels[(part_px >= 0) | (person_px >= 0)].ravel(), minlength=n + 1)
    inside = qc.sum(1) >= 0.5 * np.maximum(areas, 1)
    bg_i, obj_i = PART_NAMES.index("배경"), PART_NAMES.index("사물")
    body_idx = [PART_NAMES.index(p) for p in BODY]
    part_of, person_of = {}, {}
    background = np.zeros(n + 1, bool)
    for r in range(1, n + 1):
        if not areas[r]:
            continue
        c = pc[r]
        if not inside[r]:
            if seen[r] < 10:
                background[r] = bool(bg_hint[r])  # 지도에 거의 안 걸린 영역은 원래 배경 판단을 따른다
                continue
            if c[bg_i] >= c.sum() * 0.5 or c.sum() == 0:
                background[r] = True
                continue
            if c[obj_i] >= c[body_idx].sum():
                part_of[r] = "사물"
                continue
        body = c[body_idx]
        if body.sum():
            part_of[r] = BODY[int(body.argmax())]
        if qc[r].sum():
            person_of[r] = person_ids[int(qc[r].argmax())]
    return part_of, person_of, background


def spread(labels, region_key, todo, passes=6):
    """정하지 못한 영역은 둘레에 가장 많이 닿은 칠한 영역을 따른다 (실루엣 안 빈틈 메우기)."""
    boxes = ndimage.find_objects(labels)
    todo = [r for r in todo if boxes[r - 1] is not None]
    for _ in range(passes):
        left = []
        for r in todo:
            sl = tuple(slice(max(s.start - 6, 0), s.stop + 6) for s in boxes[r - 1])
            mine = labels[sl] == r
            ring = ndimage.binary_dilation(mine, iterations=6) & ~mine
            neigh = labels[sl][ring]
            neigh = neigh[(neigh > 0) & (region_key[neigh] >= 0)]
            if neigh.size:
                region_key[r] = region_key[np.bincount(neigh).argmax()]
            else:
                left.append(r)
        if len(left) == len(todo):
            break
        todo = left


def person_crops(labels, boundary, person_px, person_ids, chunks):
    """사람마다 잘라 낸 선화 (다른 사람·배경 선은 흐리게) — 누구인지 알아보는 데 쓴다."""
    crops = []
    for j, pid in enumerate(person_ids):
        m = person_px == j
        if m.sum() < 2000:
            continue
        ys, xs = np.nonzero(m)
        pad = 40
        y0, y1 = max(0, ys.min() - pad), min(m.shape[0], ys.max() + pad)
        x0, x1 = max(0, xs.min() - pad), min(m.shape[1], xs.max() + pad)
        sub = m[y0:y1, x0:x1]
        bnd = boundary[y0:y1, x0:x1]
        rgb = np.full(sub.shape + (3,), 255, np.uint8)
        rgb[bnd] = 190
        rgb[bnd & ndimage.binary_dilation(sub, iterations=4)] = 0
        im = Image.fromarray(rgb)
        im.thumbnail((800, 800), Image.LANCZOS)
        crops.append((pid, im))
    return crops


def identify_job(crops, chars):
    """누구인지 묻는 요청 (API·요청 폴더·MCP 공통)."""
    ids = [pid for pid, _ in crops]
    names = [c["name"] for c in chars]
    looks = "\n".join(f"- {c['name']}: {c['look'] or '(설명 없음)'}" for c in chars)
    person = match._obj({"person": {"type": "string", "enum": ids},
                         "character": {"type": "string", "enum": names + ["모름"]},
                         "reason": {"type": "string"}})
    schema = match._obj({"people": {"type": "array", "items": person}})
    prompt = IDENTIFY_PROMPT.format(ids=", ".join(ids), looks=looks)
    return match.Job(np.array([0]), [], [], [im for _, im in crops], prompt, schema, map_label="[원고 인물]")


def save_identify(job, crops, folder):
    """요청 폴더·MCP용: 인물 그림과 지시문을 남긴다."""
    folder = Path(folder)
    for pid, im in crops:
        im.convert("RGB").save(folder / f"person_{pid}.png")
    (folder / "schema.json").write_text(json.dumps(job.schema, ensure_ascii=False, indent=2))
    (folder / "characters.json").write_text(json.dumps([pid for pid, _ in crops], ensure_ascii=False))
    (folder / "README.md").write_text(
        "# psd-paint 캐릭터 알아보기 요청\n\n"
        f"이미지: {', '.join(f'person_{pid}.png' for pid, _ in crops)}\n\n"
        f"{job.prompt}\n\n"
        "답은 schema.json 형식의 JSON으로 이 폴더의 answer.json에 저장한다.\n"
        "저장한 뒤 psd-paint에서 같은 파일을 다시 실행하면 답을 적용한다.\n")


def assign(part_of, person_of, who, chars, n, labels, background):
    """부위·사람·캐릭터 → 영역별 키. 레이어는 부위마다 하나, 색은 캐릭터마다 다르다.

    부위를 정하지 못한 배경 아닌 영역은 둘레를 따른다.
    → region_key (영역 수+1), keys [(레이어, rgb)], 레이어 이름 순서
    """
    color_of = {c["name"]: c["colors"] for c in chars}
    # 엑스트라·모르는 인물의 피부는 등록한 캐릭터들이 가장 많이 쓴 피부색 (없으면 흰색)
    skins = [tuple(c["colors"]["피부"]) for c in chars if c["colors"].get("피부")]
    default_skin = max(set(skins), key=skins.count) if skins else (255, 255, 255)
    region_key = np.full(n + 1, -1, np.int64)
    keys = []
    extras = sorted({p for p in person_of.values() if who.get(p, "모름") not in color_of})
    for r, part in part_of.items():
        if part == "사물":
            key = ("사물", (215, 215, 215))
        else:
            pid = person_of.get(r)
            rgb = color_of.get(who.get(pid, "모름"), {}).get(part)
            if rgb is None:
                if part == "피부":
                    rgb = default_skin
                else:
                    # 프리셋에 없는 인물·부위: 부위별 회색, 옆 사람과 섞이지 않게 사람마다 밝기를 조금 바꾼다
                    j = extras.index(pid) if pid in extras else 0
                    tone = int(np.clip(match.PART_TONES.get(part, 200) + (j % 3 - 1) * 12, 40, 245))
                    rgb = (tone, tone, tone)
            key = (match.LAYER_OF_PART.get(part, part), tuple(int(v) for v in rgb))
        if key not in keys:
            keys.append(key)
        region_key[r] = keys.index(key)
    areas = np.bincount(labels.ravel(), minlength=n + 1)
    spread(labels, region_key, [r for r in range(1, n + 1) if areas[r] and region_key[r] < 0 and not background[r]])
    order = [match.LAYER_OF_PART.get(p, p) for p in match.PARTS] + ["사물"]
    names = sorted({k[0] for k in keys}, key=lambda s: order.index(s) if s in order else len(order))
    return region_key, keys, names
