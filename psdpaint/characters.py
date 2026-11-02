"""캐릭터 프리셋으로 칠하기. 색은 캐릭터 프리셋(색상 프리셋 '캐릭터/부위')에서 가져오므로 AI가 톤을 고르지 않는다.

부위 나누기는 두 가지다.
- 이미지 지도(image): 이미지 모델(Gemini 나노 바나나)이 컷마다 부위별·사람별 코드 색으로 칠하고, 영역마다 다수결한다.
  글 AI는 사람마다 잘라 낸 선화를 보고 누구인지만 고른다. 위치를 그림으로 바로 내놓으므로 가장 정확하다.
- 영역 확인 칸(tiles): 영역마다 빨갛게 강조한 칸을 모아 보여 주고, 글 AI(Claude 등)가 칸마다 부위와 누구인지 답한다.
  그림을 그리는 모델이 필요 없어 Claude만으로(MCP·구독) 쓸 수 있다.

누구인지 알아볼 때는 캐릭터 프로필(대표 그림 + 생김새 설명)을 함께 보여 준다.
"""
import base64
import io
import json
import os
import shutil
import sys
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
# 캐릭터 프리셋에 그 부위 색이 없을 때 대신 쓸 부위 (예: 셔츠만 입은 캐릭터의 '겉옷'으로 보인 곳은 셔츠 색)
OUTFIT_FALLBACK = {"겉옷": ["상의", "셔츠"], "상의": ["셔츠", "겉옷"], "셔츠": ["상의", "겉옷"],
                   "하의": ["치마"], "치마": ["하의"], "양말": ["신발"]}


def outfit_text(c):
    """캐릭터의 등록한 옷 부위 글 (AI에게 알려 준다)."""
    worn = [p for p in PART_ALIASES if p in c.get("colors", {}) and p not in ("머리카락", "피부")]
    return ", ".join(worn) if worn else "(등록한 옷 없음)"


def looks_text(chars):
    return "\n".join(f"- {c['name']}: {c['look'] or '(설명 없음)'} / 등록한 옷: {outfit_text(c)}" for c in chars)

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


def data_dir():
    """앱 데이터 폴더 (캐릭터 프로필 그림을 둔다)."""
    if sys.platform == "darwin":
        base = Path.home() / "Library" / "Application Support"
    elif os.name == "nt":
        base = Path(os.environ.get("APPDATA") or Path.home())
    else:
        base = Path.home() / ".local" / "share"
    return base / "psd-paint"


def load_profiles():
    """캐릭터 프로필 [{"name", "look", "image": 그림 경로 또는 ""}]"""
    path = data_dir() / "characters.json"
    try:
        items = json.loads(path.read_text())
    except (OSError, ValueError):
        return []
    out = []
    for p in items:
        if isinstance(p, dict) and str(p.get("name", "")).strip():
            img = p.get("image") or ""
            if img and not Path(img).is_absolute():
                img = str(data_dir() / "characters" / img)
            out.append({"name": p["name"].strip(), "look": p.get("look", ""), "image": img if img and Path(img).exists() else ""})
    return out


def save_profiles(profiles):
    """캐릭터 프로필을 저장한다. 대표 그림은 앱 데이터 폴더로 복사해 원본 파일이 옮겨져도 남게 한다."""
    folder = data_dir() / "characters"
    folder.mkdir(parents=True, exist_ok=True)
    items = []
    for p in profiles:
        img = p.get("image") or ""
        if img and Path(img).exists() and Path(img).parent != folder:
            dest = folder / f"{p['name']}{Path(img).suffix.lower() or '.png'}"
            shutil.copyfile(img, dest)
            img = str(dest)
        items.append({"name": p["name"], "look": p.get("look", ""), "image": Path(img).name if img else ""})
    (data_dir() / "characters.json").write_text(json.dumps(items, ensure_ascii=False, indent=1))


def from_presets(presets, looks=None, profiles=None):
    """색상 프리셋 [{"name": "하늘/머리", "rgb"}] + 생김새 {"하늘": "…"} + 프로필
    → 캐릭터 [{"name", "look", "image", "colors": {부위: rgb}}]"""
    chars = {}
    for p in presets:
        name = p.get("name") or ""
        if "/" not in name:
            continue
        who, word = (s.strip() for s in name.split("/", 1))
        part = PART_OF_WORD.get(word)
        if who and part:
            chars.setdefault(who, {"name": who, "look": "", "image": "", "colors": {}})["colors"][part] = tuple(p["rgb"])
    for who, look in (looks or {}).items():
        if who.strip():
            chars.setdefault(who.strip(), {"name": who.strip(), "look": "", "image": "", "colors": {}})["look"] = look.strip()
    for p in profiles or []:
        c = chars.setdefault(p["name"], {"name": p["name"], "look": "", "image": "", "colors": {}})
        c["look"] = p.get("look") or c["look"]
        c["image"] = p.get("image") or ""
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
    return from_presets(presets, parse_looks(s.value("character_looks", "")), load_profiles())


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


def spread(labels, region_key, todo, passes=6, reach=14):
    """정하지 못한 영역은 둘레에 가장 많이 닿은 칠한 영역을 따른다 (실루엣 안 빈틈 메우기).

    reach: 둘레를 볼 거리(px). 선이 두꺼우면 이웃 영역이 선 너머에 있으므로 선 두께보다 넉넉히 본다.
    """
    boxes = ndimage.find_objects(labels)
    todo = [r for r in todo if boxes[r - 1] is not None]
    for _ in range(passes):
        left = []
        for r in todo:
            sl = tuple(slice(max(s.start - reach, 0), s.stop + reach) for s in boxes[r - 1])
            mine = labels[sl] == r
            ring = ndimage.binary_dilation(mine, iterations=reach) & ~mine
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
    looks = looks_text(chars)
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
            colors = color_of.get(who.get(pid, "모름"), {})
            if colors and part not in colors:
                # 등록하지 않은 옷 부위면 가까운 등록 부위를 쓴다 (셔츠만 입은 캐릭터의 겉옷 → 셔츠)
                part = next((alt for alt in OUTFIT_FALLBACK.get(part, []) if alt in colors), part)
            rgb = colors.get(part)
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


# ---- 영역 확인 칸 (그림 모델 없이 글 AI만으로) ----
TILE_PARTS = BODY + ["사물", "배경"]
TILE = 300                      # 칸 하나의 크기(px)
TILE_COLS, TILE_ROWS = 5, 4
TILE_PROMPT = """웹툰 밑색 작업이다.
[레퍼런스]는 등록한 등장인물의 대표 그림이다. 순서대로 {names}이다.
[영역 확인] 그림은 칸마다 선화 일부를 잘라 낸 것이고, 칸마다 영역 하나를 빨갛게 칠했다. 칸 위의 숫자가 칸 번호다.
칸이 페이지 위에서 아래 순서라 이웃한 번호는 대개 같은 컷이다.
칸마다 빨간 영역이 무엇인지 적는다.
- part: {parts} 중 하나.
- character: 그 몸이 누구인지 등장인물 이름. 등장인물이 아니거나(엑스트라) 판단이 서지 않으면 모름, 사물·배경이면 없음.
판단 규칙:
- 둘레의 선화(얼굴·눈·귀·머리 모양·머리띠·옷깃·손가락)를 보고 그 영역이 몸의 어디인지 판단한다. 영역 안의 선(머리카락 결, 옷 주름)도 단서다.
- 사람 실루엣 안은 배경이 아니다. 머리카락 가닥 사이 좁은 틈은 머리카락, 손가락 사이로 보이는 얼굴은 피부다.
- 실루엣 밖, 컷 안의 빈 바탕과 하늘·벽·바닥은 배경이다. 말풍선·효과선 안도 배경이다.
- 손에 든 물건은 사물, 그 손은 피부다.
- 손은 꼭 피부로 적는다. 손가락 갈래·손톱·마디 선이 보이거나 소매 끝에서 나온 덩어리면 손이다 (옷 색으로 적지 않는다).
  얼굴·목·귀·팔·다리처럼 맨살도 피부다. 누구 손인지 모르겠으면 character는 모름으로 두고 part는 피부로 적는다.
- 같은 사람이 여러 칸에 나오면 같은 이름을 쓴다. 머리띠·머리 모양·귀걸이로 누구인지 알아본다.
- 옷은 그 등장인물의 '등록한 옷' 안에서 고른다. 예를 들어 셔츠만 등록한 인물은 겉옷처럼 보여도 셔츠다.
옷의 구조 (바깥에서 안으로 겹쳐 입는다):
- 겉옷(재킷·코트·가디건): 가장 바깥. 어깨, 소매 전체(소매 끝까지), 앞판, 깃(라펠), 주머니 덮개, 단추가 달린 앞섶.
- 상의(조끼·니트·스웨터): 겉옷 안, 셔츠 위. 겉옷 앞섶 사이로 V자·U자로 보이는 몸통 부분. 소매가 없거나 겉옷에 가린다.
- 셔츠: 가장 안쪽. 겉옷을 입었으면 목깃(칼라), V자 안쪽 가슴, 소매 끝으로 나온 커프스만 보인다.
  겉옷이 없으면 몸통 전체와 소매 전체가 셔츠다 (단추줄·가슴 주머니가 있는 쪽).
- 넥타이·리본: 목깃 아래 매듭과, 거기서 아래로 좁고 길게 내려오는 띠.
- 하의: 허리 아래 다리를 덮는 바지. 치마는 허리에서 퍼지는 주름. 신발은 발목 아래.
- 부위는 옷의 경계(깃 가장자리·소매 끝·솔기·허리선)에서만 바뀐다. 주름선·접힌 선·팔이나 손이 가린 곳으로 나뉜 조각은 같은 옷이다.
- 칸이 작아 헷갈리면 칸 둘레에서 그 조각이 어깨·팔·몸통 중 어디에 붙어 있는지 보고, 그 자리에 입는 옷을 고른다.
등장인물:
{looks}
칸 번호 1 ~ {count}를 모두 적는다."""


def tile_regions(labels, cand):
    """확인할 영역을 페이지 위→아래, 왼→오른 순서로. (이웃 칸이 같은 컷이 되게)"""
    boxes = ndimage.find_objects(labels)
    def key(r):
        sl = boxes[r - 1]
        return ((sl[0].start + sl[0].stop) // 120, (sl[1].start + sl[1].stop) // 2)
    return sorted((int(r) for r in cand if boxes[r - 1] is not None), key=key)


def region_sheets(labels, boundary, regions):
    """영역마다 둘레를 넉넉히 잘라 그 영역만 빨갛게 칠한 칸을 만들고 판으로 모은다. → [판 이미지]"""
    h, w = labels.shape
    boxes = ndimage.find_objects(labels)
    from PIL import ImageDraw
    font = match._font(22)
    tiles = []
    for n, r in enumerate(regions, 1):
        sl = boxes[r - 1]
        cy, cx = (sl[0].start + sl[0].stop) // 2, (sl[1].start + sl[1].stop) // 2
        # 영역보다 넉넉히 잘라 얼굴·몸이 보이게 한다 (작은 영역도 최소 280px 둘레)
        half = max(int(max(sl[0].stop - sl[0].start, sl[1].stop - sl[1].start) * 0.8), 140)
        y0, y1, x0, x1 = max(0, cy - half), min(h, cy + half), max(0, cx - half), min(w, cx + half)
        rgb = np.full((y1 - y0, x1 - x0, 3), 255, np.uint8)
        rgb[labels[y0:y1, x0:x1] == r] = (255, 120, 120)
        rgb[boundary[y0:y1, x0:x1]] = 0
        im = Image.fromarray(rgb)
        im.thumbnail((TILE, TILE), Image.LANCZOS)
        cell = Image.new("RGB", (TILE, TILE + 30), "white")
        cell.paste(im, ((TILE - im.width) // 2, 30 + (TILE - im.height) // 2))
        d = ImageDraw.Draw(cell)
        d.text((8, 4), str(n), fill=(0, 0, 0), font=font)
        d.rectangle((0, 0, TILE - 1, TILE + 29), outline=(170, 170, 170))
        tiles.append(cell)
    per = TILE_COLS * TILE_ROWS
    sheets = []
    for i in range(0, len(tiles), per):
        group = tiles[i:i + per]
        rows = (len(group) + TILE_COLS - 1) // TILE_COLS
        sheet = Image.new("RGB", (TILE_COLS * TILE, rows * (TILE + 30)), "white")
        for j, cell in enumerate(group):
            sheet.paste(cell, ((j % TILE_COLS) * TILE, (j // TILE_COLS) * (TILE + 30)))
        sheets.append(sheet)
    return sheets


def profile_images(chars):
    """대표 그림이 있는 캐릭터 → [(이름, 그림)]"""
    out = []
    for c in chars:
        if c.get("image") and Path(c["image"]).exists():
            im = Image.open(c["image"]).convert("RGB")
            im.thumbnail((700, 900), Image.LANCZOS)
            out.append((c["name"], im))
    return out


def tiles_job(sheets, chars, count):
    profiles = profile_images(chars)
    names = [c["name"] for c in chars]
    looks = looks_text(chars)
    item = match._obj({"tile": {"type": "integer"}, "part": {"type": "string", "enum": TILE_PARTS},
                       "character": {"type": "string", "enum": names + ["모름", "없음"]}})
    schema = match._obj({"items": {"type": "array", "items": item}})
    prompt = TILE_PROMPT.format(names=", ".join(n for n, _ in profiles) or "(대표 그림 없음)",
                                parts=", ".join(TILE_PARTS), looks=looks, count=count)
    return match.Job(np.array([0]), [], [im for _, im in profiles], sheets, prompt, schema, map_label="[영역 확인]"), profiles


def save_tiles(job, profiles, regions, folder):
    """요청 폴더·MCP용: 대표 그림·칸 판·지시문을 남긴다."""
    folder = Path(folder)
    folder.mkdir(parents=True, exist_ok=True)
    names = []
    for i, (name, im) in enumerate(profiles, 1):
        names.append(f"profile_{i}.png")
        im.save(folder / names[-1])
    for i, im in enumerate(job.map_images, 1):
        names.append(f"tiles_{i}.png")
        im.save(folder / names[-1])
    (folder / "tiles.json").write_text(json.dumps({"regions": regions, "profiles": [n for n, _ in profiles],
                                                   "sheets": len(job.map_images)}, ensure_ascii=False))
    (folder / "schema.json").write_text(json.dumps(job.schema, ensure_ascii=False, indent=2))
    (folder / "README.md").write_text(
        "# psd-paint 영역 확인 요청\n\n"
        f"이미지: {', '.join(names)} (profile_* = 등장인물 대표 그림, tiles_* = 영역 확인 칸)\n\n"
        f"{job.prompt}\n\n"
        "답은 schema.json 형식의 JSON으로 이 폴더의 answer.json에 저장한다.\n"
        "저장한 뒤 psd-paint에서 같은 파일을 다시 실행하면 답을 적용한다.\n")


def from_tiles(answer, regions, n):
    """칸 답 → part_of, person_of, who, background"""
    said = {int(it["tile"]): it for it in answer.get("items", [])}
    part_of, person_of, who = {}, {}, {}
    background = np.zeros(n + 1, bool)
    for t, r in enumerate(regions, 1):
        it = said.get(t)
        if not it:
            continue
        part = it.get("part")
        if part == "배경":
            background[r] = True
        elif part == "사물":
            part_of[r] = "사물"
        elif part in BODY:
            part_of[r] = part
            name = it.get("character") or "모름"
            person_of[r] = name
            who[name] = name
    return part_of, person_of, who, background


def panel_backgrounds(labels, frame, margin=8):
    """컷마다 컷 배경으로 볼 영역: 그 컷에서 가장 넓고, 컷의 위·아래·왼·오른 네 변에 모두 닿은 영역.

    인물 클로즈업처럼 얼굴이 가장 넓은 컷은 얼굴이 네 변에 다 닿지 않으므로 고르지 않는다.
    컷 바깥 페이지 여백도 같은 규칙으로 고른다. → {영역 번호}
    """
    import cv2

    panels, k = ndimage.label(~(cv2.dilate(frame.astype(np.uint8), np.ones((7, 7), np.uint8)) > 0))
    m = labels > 0
    n = int(labels.max())
    # 영역마다 가장 많이 겹친 컷
    pairs, counts = np.unique(labels[m].astype(np.int64) * (k + 1) + panels[m], return_counts=True)
    lab, pan = pairs // (k + 1), pairs % (k + 1)
    order = np.lexsort((-counts, lab))
    lab, pan, counts = lab[order], pan[order], counts[order]
    first = np.r_[True, lab[1:] != lab[:-1]]
    panel_of = np.zeros(n + 1, np.int64)
    panel_of[lab[first]] = pan[first]
    areas = np.bincount(labels.ravel(), minlength=n + 1)
    boxes = ndimage.find_objects(labels)
    pboxes = ndimage.find_objects(panels)
    out = set()
    for p in range(1, k + 1):
        regs = np.nonzero(panel_of == p)[0]
        regs = regs[regs > 0]
        if not regs.size or pboxes[p - 1] is None:
            continue
        r = int(regs[np.argmax(areas[regs])])
        rb, pb = boxes[r - 1], pboxes[p - 1]
        if (rb[0].start <= pb[0].start + margin and rb[0].stop >= pb[0].stop - margin
                and rb[1].start <= pb[1].start + margin and rb[1].stop >= pb[1].stop - margin):
            out.add(r)
    return out


# ---- 프리셋 묶음 (다른 컴퓨터·동료와 주고받기) ----
BUNDLE_SUFFIX = ".psdpaint"


def export_bundle(path, presets, profiles):
    """색상 프리셋과 캐릭터 프로필(대표 그림 포함)을 한 파일(zip)로 내보낸다."""
    import zipfile

    items = []
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as z:
        for p in profiles:
            img = p.get("image") or ""
            name = ""
            if img and Path(img).exists():
                name = f"characters/{p['name']}{Path(img).suffix.lower() or '.png'}"
                z.write(img, name)
            items.append({"name": p["name"], "look": p.get("look", ""), "image": name})
        z.writestr("psd-paint.json", json.dumps({"version": 1, "presets": presets, "characters": items},
                                                ensure_ascii=False, indent=1))


def import_bundle(path):
    """→ (presets, profiles). 대표 그림은 앱 데이터 폴더에 풀어 둔다."""
    import zipfile

    with zipfile.ZipFile(path) as z:
        try:
            data = json.loads(z.read("psd-paint.json"))
        except KeyError:
            raise ValueError("psd-paint 프리셋 파일이 아닙니다.") from None
        folder = data_dir() / "characters"
        folder.mkdir(parents=True, exist_ok=True)
        profiles = []
        for c in data.get("characters", []):
            img = ""
            if c.get("image"):
                dest = folder / Path(c["image"]).name
                dest.write_bytes(z.read(c["image"]))
                img = str(dest)
            profiles.append({"name": c["name"], "look": c.get("look", ""), "image": img})
    presets = [{"name": p["name"], "rgb": [int(v) for v in p["rgb"]]} for p in data.get("presets", [])
               if isinstance(p.get("name"), str)]
    return presets, profiles


def smooth_people(labels, person_of, part_of, pairs, share=0.8, ratio=0.2):
    """칸 답에서 '누구인지'를 바로잡는다. 한 사람 몸 안에 다른 이름으로 답한 조각(뭉치)이 박혀 있으면 둘레 사람으로 바꾼다.

    맞닿은 같은 이름 영역끼리 뭉치로 묶고, 뭉치의 바깥 이웃(몸 영역, 모름 제외) 가운데 share 이상이 한 사람이며
    그 사람 쪽 이웃 뭉치 넓이에 비해 ratio보다 작은 뭉치만 바꾼다.
    두 사람이 맞닿은 경계(각자 큰 몸)는 건드리지 않고, 몸 안에 박힌 작은 실수만 고친다. → 바꾼 영역 수
    """
    areas = np.bincount(labels.ravel(), minlength=int(labels.max()) + 1)
    neigh = {}
    for a, b in pairs:
        a, b = int(a), int(b)
        if a in person_of and b in person_of:
            neigh.setdefault(a, set()).add(b)
            neigh.setdefault(b, set()).add(a)
    # 같은 이름으로 이어진 뭉치
    group, groups = {}, []
    for r in person_of:
        if r in group:
            continue
        gid, stack, members = len(groups), [r], []
        group[r] = gid
        while stack:
            x = stack.pop()
            members.append(x)
            for o in neigh.get(x, ()):
                if o not in group and person_of[o] == person_of[r]:
                    group[o] = gid
                    stack.append(o)
        groups.append(members)
    size = [sum(int(areas[m]) for m in g) for g in groups]
    changed = 0
    for gid, members in enumerate(groups):
        me = person_of[members[0]]
        if me == "모름":
            continue
        outside = {o for m in members for o in neigh.get(m, ()) if group[o] != gid and person_of[o] != "모름"}
        if not outside:
            continue
        names = [person_of[o] for o in outside]
        best = max(set(names), key=names.count)
        if names.count(best) < share * len(names):
            continue
        support = sum(size[g] for g in {group[o] for o in outside if person_of[o] == best})
        if size[gid] < ratio * support:
            for m in members:
                person_of[m] = best
            changed += len(members)
    return changed


def check_maps(state, regions, width=900):
    """검수용 결과 지도: 칠한 색 위에 영역 확인 칸 번호를 얹는다. 컷 단위로 나눠 돌려준다. → [이미지]"""
    from PIL import ImageDraw

    labels, region_key, keys = state["labels"], state["region_key"], state["keys"]
    lut = np.array([(255, 255, 255)] + [rgb for _, rgb in keys], np.uint8)
    rgb = lut[(region_key + 1)[labels]]
    rgb[state["boundary"]] = 0
    img = Image.fromarray(rgb)
    draw = ImageDraw.Draw(img)
    font = match._font(34)
    depth = ndimage.distance_transform_edt(labels > 0)
    spots = ndimage.maximum_position(depth, labels, index=regions)
    for t, (y, x) in enumerate(spots, 1):
        draw.text((x, y), str(t), fill=(220, 0, 0), font=font, anchor="mm", stroke_width=4, stroke_fill=(255, 255, 255))
    out = []
    for y0, y1, x0, x1 in page_chunks(np.isin(labels, regions) | state["boundary"]):  # 컷 전체가 보이게
        piece = img.crop((x0, y0, x1, y1))
        if piece.width > width:
            piece = piece.resize((width, round(piece.height * width / piece.width)), Image.LANCZOS)
        out.append(piece)
    return out
