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
PERSON_CODES = {"A": (255, 0, 0), "B": (0, 0, 255), "C": (0, 200, 0), "D": (255, 160, 0), "배경": (255, 255, 255)}
KEEP = ("Keep every black line exactly where it is; do not redraw, move, add or remove lines, do not change the "
        "composition or size. Flat solid fills only (no shading, gradients or texture). Output only the image.")
PARTS_PROMPT = """This is black-and-white manga line art. Make a flat color-coded part map of it.
Fill each closed area between the lines with ONE color by what it is:
hair #FF0000, skin (face, forehead, ears, neck, hands, arms) #FFFF00, headband/hat #FF00FF,
jacket/blazer/coat incl. lapels and sleeves #0000FF, sweater/vest/knit top #00FFFF, shirt (collar, cuffs, shirt front) #00FF00,
necktie/ribbon #8000FF, pants/skirt #8B4513, shoes #404040, objects (cups, furniture, props) #FF8000, background: pure white.
Parts of the same garment separated by folds or by something in front get the same color. """ + KEEP
PERSONS_PROMPT = """This is black-and-white manga line art. Make a flat color map of the people in it.
Give each distinct person one color, from left to right: first person #FF0000, second #0000FF, third #00C800, fourth #FFA000.
Everything that belongs to a person (hair, skin, clothes, accessories, a hand reaching in) gets that person's color.
Objects that nobody wears and the background stay pure white. """ + KEEP
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


def page_chunks(kmap):
    """칠할 영역이 있는 부분을 세로 조각(컷 사이 여백에서 자름)으로 나눈다. → [(y0, y1, x0, x1)]"""
    h, w = kmap.shape
    busy = (kmap > 0).sum(1)
    ys = np.nonzero(busy)[0]
    if not ys.size:
        return []
    top, bottom = int(ys.min()), int(ys.max()) + 1
    out = []
    for a, b in match._chunks(bottom - top, busy[top:bottom]):
        y0, y1 = top + a, top + b
        cols = np.nonzero((kmap[y0:y1] > 0).any(0))[0]
        if cols.size:
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


def _votes(img, codes, kmap, n_codes_out):
    """칠한 그림의 색을 코드에 맞추고 영역마다 코드별 픽셀 수를 센다. → (영역 수+1, 코드 수) 배열"""
    cols = np.array(list(codes.values()), int)
    lab = ((img.astype(int)[:, :, None, :] - cols[None, None]) ** 2).sum(3).argmin(2)
    ok = (img.astype(int).sum(2) >= 200) & (kmap > 0)  # 선(검정)은 세지 않는다
    counts = np.zeros((n_codes_out, len(cols)), np.int64)
    np.add.at(counts, (kmap[ok], lab[ok]), 1)
    return counts


def image_maps(labels, boundary, cand, api_key, model, folder, say=lambda msg: None):
    """컷마다 부위·사람 지도를 만들고(또는 저장해 둔 것을 읽고) 영역마다 다수결한다.

    → part_of {영역: 부위}, person_of {영역: 사람 id}, crops [(사람 id, 잘라 낸 선화)], 사용량
    """
    folder = Path(folder)
    folder.mkdir(parents=True, exist_ok=True)
    n = int(labels.max())
    lut = np.zeros(n + 1, np.int32)
    lut[cand] = cand
    kmap = lut[labels]  # 후보 영역만 남긴 영역 번호
    lines = np.full(labels.shape + (3,), 255, np.uint8)
    lines[boundary] = 0
    chunks = page_chunks(kmap)
    usage = {"input": 0, "output": 0, "calls": 0, "model": model}
    jobs = []
    for i, (y0, y1, x0, x1) in enumerate(chunks, 1):
        im = Image.fromarray(lines[y0:y1, x0:x1])
        for kind, prompt in (("parts", PARTS_PROMPT), ("persons", PERSONS_PROMPT)):
            path = folder / f"{kind}_{i}.png"
            jobs.append((path, im, prompt))
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
    part_votes = np.zeros((n + 1, len(PART_CODES)), np.int64)
    person_of, crops = {}, []
    for i, (y0, y1, x0, x1) in enumerate(chunks, 1):
        km = kmap[y0:y1, x0:x1]
        part_votes += _votes(np.asarray(Image.open(folder / f"parts_{i}.png").convert("RGB")), PART_CODES, km, n + 1)
        pv = _votes(np.asarray(Image.open(folder / f"persons_{i}.png").convert("RGB")), PERSON_CODES, km, n + 1)
        regs = [int(r) for r in np.unique(km) if r and pv[r].sum() >= 20]
        names = list(PERSON_CODES)
        local = {r: names[int(pv[r].argmax())] for r in regs}
        # 조각 안의 사람: 사람 지도에서 빠졌지만 부위 지도로는 몸인 영역은 가장 가까운 사람에게 붙인다
        people = sorted({v for v in local.values() if v != "배경"})
        if people:
            dists = np.stack([ndimage.distance_transform_edt(~np.isin(km, [r for r, v in local.items() if v == p]))
                              for p in people])
            for r in regs:
                part = list(PART_CODES)[int(part_votes[r].argmax())]
                if local[r] == "배경" and part not in ("배경", "사물"):
                    ys, xs = np.nonzero(km == r)
                    local[r] = people[int(dists[:, ys, xs].mean(1).argmin())]
        for r, p in local.items():
            if p != "배경" and r not in person_of:  # 조각 둘에 걸친 영역은 먼저 본 조각을 따른다
                person_of[r] = f"{i}{p}"
        # 사람마다 잘라 낸 선화 (다른 사람·배경 선은 흐리게) — 누구인지 알아보는 데 쓴다
        bnd = boundary[y0:y1, x0:x1]
        for p in people:
            m = np.isin(km, [r for r, v in local.items() if v == p])
            if m.sum() < 2000:
                continue
            ys, xs = np.nonzero(m)
            pad = 40
            a0, a1 = max(0, ys.min() - pad), min(m.shape[0], ys.max() + pad)
            b0, b1 = max(0, xs.min() - pad), min(m.shape[1], xs.max() + pad)
            rgb = np.full(m.shape + (3,), 255, np.uint8)
            rgb[bnd] = 190
            rgb[bnd & ndimage.binary_dilation(m, iterations=3)] = 0
            im = Image.fromarray(rgb[a0:a1, b0:b1])
            im.thumbnail((800, 800), Image.LANCZOS)
            crops.append((f"{i}{p}", im))
    names = list(PART_CODES)
    part_of = {int(r): names[int(part_votes[r].argmax())] for r in cand if part_votes[r].sum() >= 20}
    return part_of, person_of, crops, usage


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


def assign(part_of, person_of, who, chars, n):
    """부위·사람·캐릭터 → 영역별 키. 레이어는 부위마다 하나, 색은 캐릭터마다 다르다.

    → region_key (영역 수+1), keys [(레이어, rgb)], 레이어 이름 순서
    """
    color_of = {c["name"]: c["colors"] for c in chars}
    region_key = np.full(n + 1, -1, np.int64)
    keys = []
    extras = sorted({p for p in person_of.values() if who.get(p, "모름") not in color_of})
    for r, part in part_of.items():
        if part == "배경":
            continue
        if part == "사물":
            key = ("사물", (215, 215, 215))
        else:
            pid = person_of.get(r)
            rgb = color_of.get(who.get(pid, "모름"), {}).get(part)
            if rgb is None:
                if part == "피부":
                    rgb = (255, 255, 255)
                else:
                    # 프리셋에 없는 인물·부위: 부위별 회색, 옆 사람과 섞이지 않게 사람마다 밝기를 조금 바꾼다
                    j = extras.index(pid) if pid in extras else 0
                    tone = int(np.clip(match.PART_TONES.get(part, 200) + (j % 3 - 1) * 12, 40, 245))
                    rgb = (tone, tone, tone)
            key = (match.LAYER_OF_PART.get(part, part), tuple(int(v) for v in rgb))
        if key not in keys:
            keys.append(key)
        region_key[r] = keys.index(key)
    order = [match.LAYER_OF_PART.get(p, p) for p in match.PARTS] + ["사물"]
    names = sorted({k[0] for k in keys}, key=lambda s: order.index(s) if s in order else len(order))
    return region_key, keys, names
