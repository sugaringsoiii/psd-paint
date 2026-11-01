"""선화 PSD에 참고 레이어 힌트대로 밑색 레이어를 만든다."""
from dataclasses import dataclass, field
from pathlib import Path

import cv2
import numpy as np
from PIL import Image
from psd_tools import PSDImage
from scipy import ndimage

from . import psdio


@dataclass
class Options:
    line_group: str = "선화"            # 선화 레이어가 든 폴더 이름
    hint_group: str = "참고"            # 힌트 폴더(또는 레이어) 이름
    hint_source: str = "layer"          # 힌트 출처: layer(파일 안 참고 레이어) · image(레퍼런스 이미지) · psd(레퍼런스 PSD)
    hint_path: str = ""                 # image·psd 방식의 레퍼런스 파일 경로
    extra_boundary: list = field(default_factory=lambda: ["컷*", "*테두리*"])  # 경계로 쓸 픽셀 레이어 이름 패턴
    output_group: str = "밑색"
    gap_close_px: int = 2       # 선을 이만큼 두껍게 해서 작은 틈을 막는다
    small_area: int = 400       # 힌트 없는 이보다 작은 영역은 이웃 영역을 따른다
    min_hint_px: int = 5        # 영역 안 힌트 픽셀이 이보다 적으면 무시한다 (선 넘은 낙서 끝 방지)
    under_line_px: int = 9      # 선 아래로 밑색을 넓히는 거리
    pocket_layers: list = field(default_factory=lambda: ["머리"])  # 잔머리 틈을 채울 레이어 이름
    pocket_gap_px: int = 6      # 잔머리 틈을 막을 때 선을 넓히는 거리
    pocket_max: int = 6000      # 이보다 넓게 갇힌 배경은 채우지 않는다
    offset_px: int = 2          # 밑색 바깥 윤곽을 안쪽으로 줄이는 거리
    auto_fill: bool = True      # 힌트 없는 영역(엑스트라 등)을 회색으로 자동 칠하기. 참고 폴더가 없어도 동작한다
    auto_layer: str = "자동"
    auto_tones: tuple = (235, 215, 195, 175, 155, 135, 115)  # 자동 칠하기 회색. 맞닿은 영역끼리 겹치지 않게 고른다
    bg_contact: float = 0.4     # 둘레의 이 비율 이상이 컷 테두리에 닿은 힌트 없는 영역은 배경으로 본다
    max_layers: int = 10        # 밑색 레이어 수 상한 (0 = 제한 없음). 넘으면 작은 레이어부터 '기타'로 합친다
    other_layer: str = "기타"
    color_tol: int = 12         # 칠한 예시 이미지에서 이 차이 안의 색은 같은 색으로 묶는다 (0~255)
    dense_min_cover: float = 0.3  # 칠한 예시·레퍼런스 PSD에서 영역의 이 비율 이상을 덮은 색만 힌트로 본다
    color_min_share: float = 0.0005  # 이보다 드문 색은 묶음을 만들지 않는다 (선 가장자리 등)
    background: list = field(default_factory=lambda: ["배경*", "*background*", "BG*", "bg*"])  # 배경 레이어·폴더 이름 패턴
    hide_background: bool = True  # 결과 PSD에서 배경 레이어·폴더를 끈다


@dataclass
class Result:
    out_path: Path
    layers: list
    regions: int
    hinted: int
    unhinted_large: int
    auto: int = 0               # 자동으로 칠한 영역 수
    merged: list = field(default_factory=list)  # '기타'로 합친 레이어
    preview: Image.Image = None  # 선화 + 밑색 축소 이미지


class PaintError(Exception):
    pass


def _disk(r):
    return cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (2 * r + 1, 2 * r + 1))


SIDECAR_SUFFIXES = ("_참고", ".참고", "_ref", "-ref")
IMAGE_EXTS = (".png", ".jpg", ".jpeg", ".webp")


def find_sidecar(psd_path):
    """PSD 옆에 놓인 참고 이미지 (예: 1화.psd → 1화_참고.png)."""
    p = Path(psd_path)
    for suf in SIDECAR_SUFFIXES:
        for ext in IMAGE_EXTS:
            cand = p.with_name(p.stem + suf + ext)
            if cand.exists():
                return cand
    return None


def load_sidecar(path, psd):
    """참고 이미지를 캔버스 크기 RGBA로. 크기가 다르면 캔버스에 맞춰 늘린다."""
    im = Image.open(path).convert("RGBA")
    if im.size != (psd.width, psd.height):
        im = im.resize((psd.width, psd.height), Image.NEAREST)
    return np.asarray(im).copy()


def quantize(rgb, tol, min_share):
    """칠한 예시 이미지의 비슷한 색(브러시 결, 안티에일리어싱)을 대표색으로 묶는다.

    → (픽셀별 대표색 번호, 대표색 RGB 목록). 아주 드문 색은 -1.
    """
    q = (rgb >> 3).astype(np.int32)
    packed = (q[:, 0] << 10) | (q[:, 1] << 5) | q[:, 2]
    bins, inverse, counts = np.unique(packed, return_inverse=True, return_counts=True)
    centers = np.stack([(bins >> 10) & 31, (bins >> 5) & 31, bins & 31], 1) * 8 + 4
    palette, bin_to = [], np.full(len(bins), -1)
    for b in np.argsort(-counts):
        c = centers[b]
        if palette:
            d = np.abs(np.array(palette) - c).max(1)
            j = int(d.argmin())
            if d[j] <= tol:
                bin_to[b] = j
                continue
        if counts[b] < min_share * len(rgb):
            continue
        palette.append(c)
        bin_to[b] = len(palette) - 1
    idx = bin_to[inverse]
    # 대표색은 묶인 픽셀의 중앙값으로 다시 잡는다
    colors = [tuple(int(v) for v in np.median(rgb[idx == j], 0)) for j in range(len(palette))]
    # 거의 흰색·검은색은 순수한 흰색·검은색으로 맞춘다
    colors = [(255,) * 3 if min(c) >= 248 else (0,) * 3 if max(c) <= 7 else c for c in colors]
    return idx, colors


def _folder_hints(group, src_psd, key_map, keys):
    """폴더의 하위 레이어마다 이름과 색을 힌트로 읽는다. 아래 레이어부터 칠해 위 레이어가 덮는다."""
    for layer in group:
        if layer.is_group():
            continue
        rgba = psdio.canvas_rgba(layer, src_psd)
        solid = rgba[..., 3] >= 250
        if not solid.any():
            continue
        packed = (rgba[..., 0].astype(np.int32) << 16) | (rgba[..., 1].astype(np.int32) << 8) | rgba[..., 2]
        colors, inverse = np.unique(packed[solid], return_inverse=True)
        base = len(keys)
        for c in colors:
            keys.append((layer.name, ((c >> 16) & 255, (c >> 8) & 255, c & 255)))
        key_map[solid] = base + inverse


def _image_hints(rgba, opts, key_map):
    """칠한 예시 이미지를 비슷한 색끼리 묶어 색마다 레이어를 나눈다."""
    keys = []
    solid = rgba[..., 3] >= 250
    if solid.any():
        idx, colors = quantize(rgba[..., :3][solid], opts.color_tol, opts.color_min_share)
        sub = np.full(idx.shape, -1, np.int32)
        sub[idx >= 0] = idx[idx >= 0]
        key_map[solid] = sub
        keys = [(f"색_{r:02X}{g:02X}{b:02X}", (r, g, b)) for r, g, b in colors]
    return keys


def _check_size(w, h, psd, what):
    if (w, h) == (psd.width, psd.height):
        return
    if abs(w / h - psd.width / psd.height) > 0.01:
        raise PaintError(f"{what} 크기({w}×{h})가 원고({psd.width}×{psd.height})와 다릅니다. "
                         "같은 페이지를 칠한 레퍼런스만 쓸 수 있습니다")


def read_hints(psd, opts, psd_path=None):
    """참고 힌트 → (힌트 키 맵, 키별 (레이어 이름, RGB), 레이어 이름 순서(위→아래), 이미지 방식 여부).

    opts.hint_source에 따라 힌트를 읽는다.
    - "layer": 파일 안 참고 폴더(점·낙서, 하위 레이어 이름이 밑색 레이어 이름) 또는 참고 레이어 하나(칠한 예시).
      둘 다 없으면 PSD 옆 참고 이미지(1화_참고.png)를 찾는다.
    - "image": opts.hint_path의 PNG·JPG. 같은 페이지를 칠한 예시로 보고 비슷한 색끼리 묶는다.
    - "psd": opts.hint_path의 레퍼런스 PSD. 그 안의 밑색(또는 참고) 폴더 레이어 이름과 색을 그대로 쓴다.
    """
    key_map = np.full((psd.height, psd.width), -1, np.int32)
    keys = []
    image_mode = False

    if opts.hint_source == "image":
        if not opts.hint_path or not Path(opts.hint_path).exists():
            raise PaintError("레퍼런스 이미지를 지정하세요")
        im = Image.open(opts.hint_path)
        _check_size(*im.size, psd, "레퍼런스 이미지")
        keys = _image_hints(load_sidecar(opts.hint_path, psd), opts, key_map)
        image_mode = True
    elif opts.hint_source == "psd":
        if not opts.hint_path or not Path(opts.hint_path).exists():
            raise PaintError("레퍼런스 PSD를 지정하세요")
        ref = PSDImage.open(opts.hint_path)
        _check_size(ref.width, ref.height, psd, "레퍼런스 PSD")
        group = psdio.find(ref, opts.output_group, True) or psdio.find(ref, opts.hint_group, True)
        if group is not None:
            _folder_hints(group, ref, key_map, keys)
        else:
            # 밑색 폴더가 없으면 선화를 끈 합성 이미지를 칠한 예시로 쓴다
            line = psdio.find(ref, opts.line_group, True)
            with psdio.hidden([line] if line is not None else []):
                rgba = np.asarray(ref.composite(force=True).convert("RGBA")).copy()
            keys = _image_hints(rgba, opts, key_map)
            image_mode = True
    else:
        hint = psdio.find(psd, opts.hint_group)
        if hint is not None and hint.is_group():
            _folder_hints(hint, psd, key_map, keys)
            if not keys and not opts.auto_fill:
                raise PaintError(f"'{opts.hint_group}' 폴더 안에 힌트가 없습니다")
        elif hint is not None:
            keys = _image_hints(psdio.canvas_rgba(hint, psd), opts, key_map)
            image_mode = True
        elif psd_path and (sidecar := find_sidecar(psd_path)):
            keys = _image_hints(load_sidecar(sidecar, psd), opts, key_map)
            image_mode = True
        elif not opts.auto_fill:
            raise PaintError(f"'{opts.hint_group}' 힌트 폴더·레이어나 참고 이미지가 없습니다")

    if image_mode:
        # 넓게 칠한 색부터 위에 둔다
        area = np.bincount(key_map[key_map >= 0], minlength=len(keys))
        names_top_down = [keys[k][0] for k in np.argsort(-area)]
    else:
        names_top_down = list(dict.fromkeys(n for n, _ in reversed(keys)))
    return key_map, keys, names_top_down, image_mode


def segment(psd, opts):
    """선화와 컷 테두리로 닫힌 영역을 나눈다. → (선화 폴더, 경계, 컷 테두리, 영역 번호 맵, 영역 수)"""
    line_group = psdio.find(psd, opts.line_group, True)
    if line_group is None:
        raise PaintError(f"'{opts.line_group}' 선화 폴더가 없습니다")
    # 선화 폴더 안의 배경 선은 경계에서 뺀다 (배경이 잘게 쪼개지지 않게)
    with psdio.hidden(psdio.matching(line_group, opts.background)):
        line = psdio.group_alpha(line_group, psd) > 80
    frame = np.zeros_like(line)
    for layer in psdio.find_pixel_layers(psd, opts.extra_boundary):
        # 숨긴 레이어, 배경, 캔버스 절반 이상을 덮는 레이어(컷 배경 등)는 경계가 아니다
        if not layer.is_visible() or psdio.matches(layer.name, opts.background):
            continue
        a = psdio.canvas_rgba(layer, psd)[..., 3] > 80
        if a.mean() < 0.5:
            frame |= a
    boundary = line | frame
    closed = cv2.dilate(boundary.astype(np.uint8), _disk(1), iterations=opts.gap_close_px) > 0
    labels, n = ndimage.label(~closed)
    return line_group, boundary, frame, labels, n


def assign_hints(labels, n, key_map, opts, dense=False):
    """영역마다 가장 많이 칠해진 힌트 키. 힌트가 없으면 -1.

    dense(칠한 예시·레퍼런스)면 영역의 dense_min_cover 이상을 덮은 색만 인정한다.
    선 아래로 넘친 칠이 배경 가장자리에 걸쳐도 배경을 칠하지 않게 하기 위해서다.
    """
    region_key = np.full(n + 1, -1, np.int64)
    m = (key_map >= 0) & (labels > 0)
    if m.any():
        k_count = int(key_map.max()) + 1
        pairs = labels[m].astype(np.int64) * k_count + key_map[m]
        uniq, counts = np.unique(pairs, return_counts=True)
        lab, key = uniq // k_count, uniq % k_count
        order = np.lexsort((-counts, lab))
        lab, key, counts = lab[order], key[order], counts[order]
        first = np.r_[True, lab[1:] != lab[:-1]]
        ok = first & (counts >= opts.min_hint_px)
        if dense:
            areas = np.bincount(labels.ravel(), minlength=n + 1)
            ok &= counts >= opts.dense_min_cover * areas[lab]
        region_key[lab[ok]] = key[ok]
    return region_key


def find_background(labels, n, frame, region_key, opts, unhinted=None):
    """힌트 없는 영역 중 배경으로 볼 영역. 컷마다 가장 큰 영역, 그리고 둘레 대부분이 컷 테두리에 닿은 영역."""
    areas = np.bincount(labels.ravel(), minlength=n + 1)
    unhinted = (region_key < 0) if unhinted is None else unhinted.copy()
    unhinted[0] = False
    bg = np.zeros(n + 1, bool)

    # 컷 = 컷 테두리로 나뉜 칸 (테두리가 없으면 페이지 전체)
    panels, _ = ndimage.label(~(cv2.dilate(frame.astype(np.uint8), _disk(3)) > 0))
    m = labels > 0
    pairs = np.unique(labels[m].astype(np.int64) * (panels.max() + 1) + panels[m], return_counts=True)
    lab, pan = pairs[0] // (panels.max() + 1), pairs[0] % (panels.max() + 1)
    order = np.lexsort((-pairs[1], lab))
    lab, pan = lab[order], pan[order]
    first = np.r_[True, lab[1:] != lab[:-1]]
    region_panel = np.zeros(n + 1, np.int64)
    region_panel[lab[first]] = pan[first]
    for p in np.unique(region_panel[1:]):
        cand = np.nonzero(unhinted & (region_panel == p))[0]
        if cand.size:
            bg[cand[np.argmax(areas[cand])]] = True

    # 컷 구석처럼 둘레 대부분이 테두리에 닿은 영역
    edge = (labels > 0) & (cv2.dilate((labels == 0).astype(np.uint8), _disk(1)) > 0)
    near_frame = cv2.dilate(frame.astype(np.uint8), _disk(opts.gap_close_px + 3)) > 0
    perim = np.bincount(labels[edge], minlength=n + 1)
    touch = np.bincount(labels[edge & near_frame], minlength=n + 1)
    bg |= unhinted & (touch >= opts.bg_contact * np.maximum(perim, 1)) & (touch > 0)
    return bg


def adjacency(labels, reach=12):
    """선을 사이에 두고 맞닿은 영역 쌍 (a < b)."""
    pairs = set()
    h, w = labels.shape
    for dy, dx in ((0, reach), (reach, 0), (reach, reach), (reach, -reach)):
        a = labels[:h - dy, max(0, -dx):w - max(0, dx)]
        b = labels[dy:, max(0, dx):w - max(0, -dx)]
        m = (a > 0) & (b > 0) & (a != b)
        if m.any():
            lo, hi = np.minimum(a[m], b[m]), np.maximum(a[m], b[m])
            pairs.update(zip(*np.unique(np.stack([lo, hi]), axis=1)))
    return pairs


def auto_color(labels, n, region_key, bg, keys, opts):
    """힌트도 배경도 아닌 큰 영역에 회색을 고른다. 맞닿은 영역과 같은 회색은 피한다. → 자동으로 칠한 영역 수"""
    areas = np.bincount(labels.ravel(), minlength=n + 1)
    todo = np.nonzero((region_key < 0) & ~bg & (areas >= opts.small_area))[0]
    todo = todo[todo > 0]
    if not todo.size:
        return 0
    base = len(keys)
    keys.extend((opts.auto_layer, (t, t, t)) for t in opts.auto_tones)
    tone_of = {}  # 영역 → 회색 값
    for k in np.nonzero(region_key >= 0)[0]:
        tone_of[k] = keys[region_key[k]][1][0]
    neigh = {}
    for a, b in adjacency(labels):
        neigh.setdefault(int(a), []).append(int(b))
        neigh.setdefault(int(b), []).append(int(a))
    rng = np.random.default_rng(0)
    for i in todo[np.argsort(-areas[todo])]:  # 큰 영역부터
        used = {tone_of.get(j) for j in neigh.get(int(i), [])}
        choices = [t for t in opts.auto_tones if t not in used] or list(opts.auto_tones)
        t = choices[rng.integers(len(choices))]
        tone_of[int(i)] = t
        region_key[i] = base + opts.auto_tones.index(t)
    return int(todo.size)


def merge_small(labels, n, region_key, opts):
    """힌트 없는 작은 영역은 둘레 12px 안에서 가장 많이 맞닿은 칠한 영역을 따른다."""
    areas = np.bincount(labels.ravel(), minlength=n + 1)
    boxes = ndimage.find_objects(labels)
    for i in range(1, n + 1):
        if region_key[i] >= 0 or areas[i] >= opts.small_area or boxes[i - 1] is None:
            continue
        sl = tuple(slice(max(s.start - 12, 0), s.stop + 12) for s in boxes[i - 1])
        mine = labels[sl] == i
        ring = ndimage.binary_dilation(mine, iterations=12) & ~mine
        neigh = labels[sl][ring]
        neigh = neigh[(neigh > 0) & (region_key[neigh] >= 0)]
        if neigh.size:
            region_key[i] = region_key[np.bincount(neigh).argmax()]


def cap_layers(cls, keys, names_top_down, opts):
    """레이어가 max_layers를 넘으면 칠한 면적이 작은 레이어부터 '기타'로 합친다. → 합친 레이어 이름"""
    if not opts.max_layers:
        return []
    count = np.bincount(cls.ravel(), minlength=len(keys) + 1)
    area = {}
    for k, (name, _) in enumerate(keys, 1):
        area[name] = area.get(name, 0) + int(count[k])
    used = [nm for nm in names_top_down if area.get(nm, 0) > 0]
    if len(used) <= opts.max_layers:
        return []
    keep = set(sorted(used, key=lambda nm: -area[nm])[:opts.max_layers - 1])
    merged = [nm for nm in used if nm not in keep]
    for k, (name, rgb) in enumerate(keys):
        if name in merged:
            keys[k] = (opts.other_layer, rgb)
    at = names_top_down.index(merged[0])
    rest = [nm for nm in names_top_down if nm not in merged]
    rest.insert(min(at, len(rest)), opts.other_layer)
    names_top_down[:] = list(dict.fromkeys(rest))
    return merged


def fill(labels, boundary, region_key, keys, opts):
    """영역 키를 픽셀로 펼치고 선 아래 넓히기·잔머리 틈 채우기·윤곽 줄이기를 한다. 0 = 빈칸, k+1 = 키 k."""
    cls = (region_key + 1).astype(np.int32)[labels]
    cls[labels == 0] = 0

    # 선(경계) 아래로 밑색을 넓혀 빈틈을 없앤다
    dist, (iy, ix) = ndimage.distance_transform_edt(cls == 0, return_indices=True)
    grow = (labels == 0) & (dist <= opts.under_line_px)
    cls[grow] = cls[iy[grow], ix[grow]]

    # 잔머리 끝은 선이 열려 배경으로 분류된다. 선을 더 두껍게 했을 때 큰 배경과 끊기는
    # 작은 틈 중 대상 레이어에 맞닿은 것만 채운다 (바깥 윤곽 밖은 큰 배경에 이어져 있어 칠하지 않는다)
    pocket_ks = {k + 1 for k, (name, _) in enumerate(keys) if name in opts.pocket_layers}
    if pocket_ks:
        bg = cls == 0
        thick = cv2.dilate(boundary.astype(np.uint8), _disk(opts.pocket_gap_px)) > 0
        pockets, _ = ndimage.label(bg & ~thick)
        area = np.bincount(pockets.ravel())
        walk = opts.pocket_gap_px + 2
        # 큰 배경에서 같은 거리만큼 넓힌 곳은 틈 입구 바깥이므로 채우지 않는다
        open_bg = np.isin(pockets, np.nonzero(area > opts.pocket_max)[0]) & (pockets > 0)
        open_bg = ndimage.binary_dilation(open_bg, iterations=walk, mask=bg & ~boundary)
        for i, sl in enumerate(ndimage.find_objects(pockets), 1):
            if sl is None or area[i] > opts.pocket_max:
                continue
            sl = tuple(slice(max(s.start - walk, 0), s.stop + walk) for s in sl)
            reach = ndimage.binary_dilation(pockets[sl] == i, iterations=walk, mask=bg[sl] & ~boundary[sl])
            ring = ndimage.binary_dilation(reach, iterations=3) & ~reach
            neigh = cls[sl][ring & ~bg[sl]]
            if not neigh.size:
                continue
            top = np.bincount(neigh).argmax()
            if top in pocket_ks and (neigh == top).sum() > 0.5 * neigh.size:
                cls[sl][reach & bg[sl] & ~open_bg[sl]] = top

    # 밑색 전체의 바깥 윤곽만 안쪽으로 줄인다 (부위끼리 맞닿은 경계는 그대로)
    if opts.offset_px:
        cls[cv2.erode((cls > 0).astype(np.uint8), _disk(opts.offset_px)) == 0] = 0
    return cls


def write(psd, line_group, cls, keys, names_top_down, opts, out_path):
    parent = line_group.parent
    group = psdio.new_group(parent, opts.output_group)
    lut_rgb = np.array([(0, 0, 0)] + [rgb for _, rgb in keys], np.uint8)
    key_names = np.array([None] + [name for name, _ in keys], object)
    written = []
    for name in reversed(names_top_down):  # 아래 레이어부터 넣는다
        ks = np.nonzero(key_names == name)[0]
        m = np.isin(cls, ks)
        if not m.any():
            continue
        ys, xs = np.nonzero(m)
        y0, y1, x0, x1 = ys.min(), ys.max() + 1, xs.min(), xs.max() + 1
        sub = m[y0:y1, x0:x1]
        rgba = np.zeros(sub.shape + (4,), np.uint8)
        rgba[..., :3] = lut_rgb[cls[y0:y1, x0:x1]]
        rgba[..., 3] = sub * 255
        rgba[~sub, :3] = 0
        psdio.new_pixel_layer(group, Image.fromarray(rgba, "RGBA"), name, int(y0), int(x0))
        written.append(name)
    # 밑색 폴더를 선화 폴더 바로 아래로
    parent.remove(group)
    parent.insert(parent.index(line_group), group)
    hint = psdio.find(psd, opts.hint_group)
    if hint is not None:
        hint.visible = False
    if opts.hide_background:
        for layer in psdio.matching(psd, opts.background):
            layer.visible = False
    psd.save(out_path)
    return list(reversed(written))


def make_preview(cls, boundary, keys, width=480):
    lut = np.array([(255, 255, 255)] + [rgb for _, rgb in keys], np.uint8)
    rgb = lut[cls]
    rgb[boundary] = 0
    im = Image.fromarray(rgb)
    return im.resize((width, max(1, round(im.height * width / im.width))), Image.LANCZOS)


def paint(psd_path, out_path, opts=None, progress=None):
    """psd_path에 힌트대로 밑색을 넣어 out_path로 저장한다. progress(단계 설명, 0~1)를 부른다."""
    opts = opts or Options()
    say = progress or (lambda msg, frac: None)
    say("PSD 여는 중", 0.0)
    psd = PSDImage.open(psd_path)
    say("선화 영역 나누는 중", 0.15)
    line_group, boundary, frame, labels, n = segment(psd, opts)
    say("참고 레이어 읽는 중", 0.3)
    key_map, keys, names, image_mode = read_hints(psd, opts, psd_path)
    say("영역에 힌트 대응하는 중", 0.4)
    region_key = assign_hints(labels, n, key_map, opts, dense=image_mode or opts.hint_source == "psd")
    if image_mode:
        # 칠한 예시 이미지는 배경도 흰색으로 칠해져 있다. 흰색 영역 중 배경으로 보이는 곳은 비운다
        white = np.array([min(rgb) >= 245 for _, rgb in keys] + [False])
        bg = find_background(labels, n, frame, region_key, opts, unhinted=(region_key < 0) | white[region_key])
        region_key[bg] = -1
    hinted = int((region_key[1:] >= 0).sum())
    auto = 0
    if opts.auto_fill:
        say("힌트 없는 영역 자동 칠하는 중", 0.5)
        bg = find_background(labels, n, frame, region_key, opts)
        auto = auto_color(labels, n, region_key, bg, keys, opts)
        if auto:
            names.append(opts.auto_layer)
    merge_small(labels, n, region_key, opts)
    areas = np.bincount(labels.ravel(), minlength=n + 1)
    unhinted_large = int(((region_key < 0) & (areas >= opts.small_area))[1:].sum())
    if not (region_key >= 0).any():
        raise PaintError("칠할 영역이 없습니다 (참고 힌트도 자동 칠하기도 없음)")
    say("밑색 채우는 중", 0.6)
    cls = fill(labels, boundary, region_key, keys, opts)
    merged = cap_layers(cls, keys, names, opts)
    say("PSD 저장 중", 0.85)
    layers = write(psd, line_group, cls, keys, names, opts, out_path)
    preview = make_preview(cls, boundary, keys)
    say("완료", 1.0)
    return Result(Path(out_path), layers, n, hinted, unhinted_large, auto, merged, preview)
