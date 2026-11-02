"""선화 PSD에 참고 레이어 힌트대로 밑색 레이어를 만든다."""
import json
import os
import shutil
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
                                        # · characters(캐릭터 프리셋 + AI)
    hint_path: str = ""                 # image·psd 방식의 레퍼런스 파일 경로
    ai_mode: str = "off"                # 다른 페이지 레퍼런스 처리: off(멈춤) · api(AI API에 묻기) · files(요청 폴더로 내보내기)
    ai_provider: str = "claude"
    ai_model: str = "claude-opus-5-5"
    ai_key: str = ""                    # 실행할 때만 넘긴다. 설정 파일에 저장하지 않는다
    ai_review: bool = False             # 칠한 결과를 AI가 한 번 더 보고 틀린 곳을 고친다 (요청 1회 추가).
                                        # 구조화 매칭에서는 없는 오류를 지어내 오히려 망치는 일이 있어 기본으로 끈다
    ai_zoom: bool = True                # 인물마다 크게 잘라 부위 나누기를 다시 검토한다 (요청 1회 추가)
    ai_check: bool = True               # 넓은 영역을 하나씩 강조해 머리카락·피부·옷을 다시 확인한다 (요청 1회 추가)
    characters: list = field(default_factory=list)  # 캐릭터 프리셋 [{"name", "look", "colors": {부위: rgb}}]
    ai_image_model: str = "gemini-3.1-flash-lite-image"  # 캐릭터 방식에서 부위·사람을 칠해 나누는 이미지 모델 (Gemini)
    ai_image_key: str = ""              # 그 이미지 모델용 Gemini API 키 (실행할 때만 넘긴다)
    ai_parts: str = "image"             # 캐릭터 방식의 부위 나누기: image(Gemini 이미지 지도) · tiles(글 AI가 영역 확인 칸에 답)
    tile_min_area: int = 1000           # 영역 확인 칸에 넣을 최소 넓이(px). 더 작은 영역은 묻지 않고 둘레를 따른다 (토큰 절약)
    tile_auto_bg: bool = True           # 컷마다 둘레를 다 두른 가장 넓은 영역(컷 배경)은 묻지 않고 배경으로 둔다
    leak_gap_px: int = 16               # 큰 영역만 이만큼 틈을 더 닫아 다시 나눈다 (옆얼굴 선이 열려 얼굴과 배경이 한 영역일 때). 0이면 끔
    leak_min_area: int = 20000          # 이보다 넓은 영역만 다시 나눈다
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
    ai_usage: dict = field(default_factory=dict)  # AI 매칭을 썼다면 모델과 토큰 수
    state: dict = field(default_factory=dict, repr=False)  # 클릭 수정·다시 저장에 쓰는 칠하기 상태


class PaintError(Exception):
    pass


class AwaitingAnswer(PaintError):
    """AI 매칭 요청 폴더를 만들었고 답(answer.json)을 기다린다."""


class LineMismatch(PaintError):
    """레퍼런스가 다른 페이지다. AI 매칭에 넘길 레퍼런스 그림(RGB)과, 있으면 이름 붙은 밑색 레이어를 담는다."""

    def __init__(self, msg, ref_rgb=None, layers=None):
        super().__init__(msg)
        self.ref_rgb = ref_rgb
        self.layers = layers  # [(레이어 이름, RGBA 배열)] 또는 None


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


def _flatten(rgba):
    """투명한 곳을 흰색으로 채운 RGB."""
    a = rgba[..., 3:4].astype(np.float32) / 255
    return (rgba[..., :3] * a + 255 * (1 - a)).astype(np.uint8)


def check_lines(rgba, boundary, what, ref_rgb=None, layers=None):
    """레퍼런스에 그려진 선이 원고 선화와 겹치는지 본다. 다른 페이지를 칠한 레퍼런스면 멈춘다."""
    dark = (rgba[..., 3] >= 250) & (rgba[..., :3].max(-1) < 80)
    # 진한 색 점·면(신발, 양말 힌트 등)은 빼고 가는 선만 본다
    dark &= ~(cv2.morphologyEx(dark.astype(np.uint8), cv2.MORPH_OPEN, _disk(3)) > 0)
    if dark.sum() < 2000:  # 선 없이 색만 칠한 레퍼런스는 확인할 수 없다
        return
    near = cv2.dilate(boundary.astype(np.uint8), _disk(3)) > 0
    ratio = (dark & near).sum() / dark.sum()
    if ratio < 0.4:
        raise LineMismatch(f"{what}의 선이 이 원고의 선화와 맞지 않습니다 (겹침 {ratio:.0%}). "
                           "다른 페이지를 칠한 레퍼런스로 보입니다",
                           _flatten(rgba) if ref_rgb is None else ref_rgb, layers)


def _folder_hints(group, src_psd, key_map, keys, boundary=None):
    """폴더의 하위 레이어마다 이름과 색을 힌트로 읽는다. 아래 레이어부터 칠해 위 레이어가 덮는다."""
    for layer in group:
        if layer.is_group():
            continue
        rgba = psdio.canvas_rgba(layer, src_psd)
        if boundary is not None:
            check_lines(rgba, boundary, f"참고 레이어 '{layer.name}'")
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


def read_hints(psd, opts, psd_path=None, boundary=None):
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
    source = opts.hint_source
    if source in ("image", "psd") and opts.hint_path:
        # 방식과 파일 종류가 어긋나면(레퍼런스 PSD를 골랐는데 PNG 등) 파일 확장자를 따른다
        source = "psd" if Path(opts.hint_path).suffix.lower() in (".psd", ".psb") else "image"

    if source == "image":
        if not opts.hint_path or not Path(opts.hint_path).exists():
            raise PaintError("레퍼런스 이미지를 지정하세요.")
        try:
            im = Image.open(opts.hint_path).convert("RGBA")
        except Exception as e:
            raise PaintError(f"레퍼런스 이미지를 읽지 못했습니다: {Path(opts.hint_path).name} ({e})") from None
        if im.size != (psd.width, psd.height) and abs(im.width / im.height - psd.width / psd.height) > 0.01:
            raise LineMismatch("레퍼런스 이미지 크기가 원고와 달라 다른 페이지로 보입니다", _flatten(np.asarray(im)))
        rgba = load_sidecar(opts.hint_path, psd)
        if boundary is not None:
            check_lines(rgba, boundary, "레퍼런스 이미지")
        keys = _image_hints(rgba, opts, key_map)
        image_mode = True
    elif source == "psd":
        if not opts.hint_path or not Path(opts.hint_path).exists():
            raise PaintError("레퍼런스 PSD를 지정하세요.")
        ref = open_psd(opts.hint_path, "레퍼런스 PSD")
        group = psdio.find(ref, opts.output_group, True) or psdio.find(ref, opts.hint_group, True)
        ref_lines = line_groups(ref, opts)
        same_size = (ref.width, ref.height) == (psd.width, psd.height)
        lines_ok = bool(ref_lines)  # 선화가 없는 PSD(캐릭터 시트 등)는 다른 페이지로 본다
        if same_size and ref_lines and boundary is not None:
            lines = np.zeros((ref.height, ref.width, 4), np.uint8)
            lines[..., 3] = np.where(lines_alpha(ref_lines, ref, opts), 255, 0)
            try:
                check_lines(lines, boundary, "레퍼런스 PSD")
            except LineMismatch:
                lines_ok = False
        if not same_size or not lines_ok:
            ref_rgb = np.asarray(ref.composite(force=True).convert("RGB")).copy()
            layers = [(l.name, psdio.canvas_rgba(l, ref)) for l in group if not l.is_group()] if group else None
            raise LineMismatch("레퍼런스 PSD가 이 원고와 다른 페이지로 보입니다", ref_rgb, layers)
        if group is not None:
            _folder_hints(group, ref, key_map, keys)
        else:
            # 밑색 폴더가 없으면 선화를 끈 합성 이미지를 칠한 예시로 쓴다
            with psdio.hidden(ref_lines):
                rgba = np.asarray(ref.composite(force=True).convert("RGBA")).copy()
            keys = _image_hints(rgba, opts, key_map)
            image_mode = True
    else:
        hint = psdio.find(psd, opts.hint_group)
        if hint is not None and hint.is_group():
            _folder_hints(hint, psd, key_map, keys, boundary)
            if not keys and not opts.auto_fill:
                raise PaintError(f"'{opts.hint_group}' 폴더 안에 힌트가 없습니다.")
        elif hint is not None:
            rgba = psdio.canvas_rgba(hint, psd)
            if boundary is not None:
                check_lines(rgba, boundary, f"참고 레이어 '{hint.name}'")
            keys = _image_hints(rgba, opts, key_map)
            image_mode = True
        elif psd_path and (sidecar := find_sidecar(psd_path)):
            keys = _image_hints(load_sidecar(sidecar, psd), opts, key_map)
            image_mode = True
        elif not opts.auto_fill:
            raise PaintError(f"'{opts.hint_group}' 힌트 폴더·레이어나 참고 이미지가 없습니다.")

    if image_mode:
        # 넓게 칠한 색부터 위에 둔다
        area = np.bincount(key_map[key_map >= 0], minlength=len(keys))
        names_top_down = [keys[k][0] for k in np.argsort(-area)]
    else:
        names_top_down = list(dict.fromkeys(n for n, _ in reversed(keys)))
    return key_map, keys, names_top_down, image_mode


def line_groups(psd, opts):
    """선화 폴더들. 같은 이름 폴더가 여러 개면 빈 폴더는 빼고 모두 쓴다. → [폴더], 첫째가 밑색을 넣을 기준 폴더"""
    groups = psdio.find_all(psd, opts.line_group, True)
    filled = [g for g in groups if psdio.has_pixels(g)]
    return filled or groups[:1]


def lines_alpha(groups, psd, opts):
    """선화 폴더들을 합친 선 마스크. 선화 폴더 안의 배경 선은 뺀다 (배경이 잘게 쪼개지지 않게)."""
    line = np.zeros((psd.height, psd.width), bool)
    for g in groups:
        with psdio.hidden(psdio.matching(g, opts.background)):
            line |= psdio.group_alpha(g, psd) > 80
    return line


def close_frames(frame, band=8, cover=0.6, min_side=150):
    """컷 테두리의 끊긴 곳을 잇는다. 머리카락이 테두리 밖으로 나가며 테두리를 지운 자리로
    컷 안의 영역(얼굴 등)이 페이지 여백과 한 영역으로 이어지는 것을 막는다.

    테두리 덩어리마다 네 변 중 대부분(cover 이상)이 테두리인 곧은 변만 끝까지 이어 그린다 (사선 컷은 건드리지 않는다).
    """
    lab, k = ndimage.label(frame)
    out = frame.copy()
    for i, sl in enumerate(ndimage.find_objects(lab), 1):
        if sl is None:
            continue
        y0, y1, x0, x1 = sl[0].start, sl[0].stop, sl[1].start, sl[1].stop
        if y1 - y0 < min_side or x1 - x0 < min_side:
            continue
        m = lab[sl] == i
        edges = {"top": m[:band].any(0), "bottom": m[-band:].any(0), "left": m[:, :band].any(1), "right": m[:, -band:].any(1)}
        width = max(2, int(np.median([
            m[:band].sum(0)[edges["top"]].mean() if edges["top"].any() else 2,
            m[:, :band].sum(1)[edges["left"]].mean() if edges["left"].any() else 2])))
        for side, hit in edges.items():
            if hit.mean() < cover:
                continue
            if side == "top":
                out[y0:y0 + width, x0:x1] = True
            elif side == "bottom":
                out[y1 - width:y1, x0:x1] = True
            elif side == "left":
                out[y0:y1, x0:x0 + width] = True
            else:
                out[y0:y1, x1 - width:x1] = True
    return out


def segment(psd, opts):
    """선화와 컷 테두리로 닫힌 영역을 나눈다. → (선화 폴더, 경계, 컷 테두리, 영역 번호 맵, 영역 수)"""
    groups = line_groups(psd, opts)
    if not groups:
        raise PaintError(f"'{opts.line_group}' 선화 폴더가 없습니다.")
    line_group = groups[0]
    line = lines_alpha(groups, psd, opts)
    frame = np.zeros_like(line)
    for layer in psdio.find_pixel_layers(psd, opts.extra_boundary):
        # 숨긴 레이어, 배경, 캔버스 절반 이상을 덮는 레이어(컷 배경 등)는 경계가 아니다
        if not layer.is_visible() or psdio.matches(layer.name, opts.background):
            continue
        a = psdio.canvas_rgba(layer, psd)[..., 3] > 80
        if a.mean() < 0.5:
            frame |= a
    frame = close_frames(frame)
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


def open_psd(path, what="PSD"):
    """PSD를 연다. PSD가 아니거나 깨진 파일이면 알아보기 쉬운 PaintError로 바꾼다."""
    try:
        return PSDImage.open(path)
    except Exception as e:
        raise PaintError(f"{what}를 읽지 못했습니다: {Path(path).name}. "
                         f"Photoshop·클립스튜디오에서 PSD로 다시 저장해 보세요. ({e})") from None


def backup_path(psd_path):
    p = Path(psd_path)
    return p.with_name(f"{p.stem}_백업{p.suffix}")


def write(psd, line_group, cls, keys, names_top_down, opts, out_path):
    # 이미 칠한 파일(원본 덮어쓰기·결과 다시 칠하기)이면 전에 만든 밑색 폴더를 새것으로 바꾼다
    while (old := psdio.find(psd, opts.output_group, True)) is not None:
        old.parent.remove(old)
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
    # 임시 파일에 다 쓴 뒤 바꿔 넣는다. 저장 중에 멈춰도 원래 파일이 깨지지 않는다
    out_path = Path(out_path)
    tmp = out_path.with_name(f".{out_path.name}.tmp")
    try:
        psd.save(tmp)
        os.replace(tmp, out_path)
    finally:
        tmp.unlink(missing_ok=True)
    return list(reversed(written))


PREVIEW_WIDTH = 1200  # 앱에서 확대해도 흐려지지 않을 만큼


def make_preview(cls, boundary, keys, width=PREVIEW_WIDTH):
    lut = np.array([(255, 255, 255)] + [rgb for _, rgb in keys], np.uint8)
    rgb = lut[cls]
    rgb[boundary] = 0
    im = Image.fromarray(rgb)
    return im.resize((width, max(1, round(im.height * width / im.width))), Image.LANCZOS)


def ai_match(mismatch, labels, n, boundary, frame, opts, out_path, say):
    """다른 페이지 레퍼런스를 AI로 대응한다. → (영역별 키, 키 목록, 레이어 이름 순서, 토큰 사용량)"""
    from . import match

    areas = np.bincount(labels.ravel(), minlength=n + 1)
    bg = find_background(labels, n, frame, np.full(n + 1, -1, np.int64), opts)
    cand = np.nonzero((areas >= opts.small_area) & ~bg)[0]
    cand = cand[cand > 0]
    if mismatch.layers:
        # 레퍼런스의 자동·기타 레이어 색은 부위를 뜻하지 않으므로 팔레트에서 뺀다
        layers = [(nm, a) for nm, a in mismatch.layers if nm not in (opts.auto_layer, opts.other_layer, "사물")]
        palette, masks = match.palette_from_layers(layers)
    else:
        rgba = np.dstack([mismatch.ref_rgb, np.full(mismatch.ref_rgb.shape[:2], 255, np.uint8)])
        palette, masks = match.palette_from_image(rgba, quantize, opts.color_tol, opts.color_min_share)
    if not palette:
        raise PaintError("레퍼런스에서 밑색을 찾지 못했습니다.")
    job = match.make_job(labels, boundary, cand, mismatch.ref_rgb, palette, masks, bg_mask=bg[labels])

    if opts.ai_mode == "files":
        folder = Path(out_path).with_name(Path(out_path).stem + "_AI매칭")
        answer_file = folder / "answer.json"
        if not answer_file.exists():
            match.save_job(job, folder)
            raise AwaitingAnswer(f"AI 매칭 요청을 만들었습니다: {folder}. "
                             "Claude Code·Codex에서 이 폴더의 README.md대로 answer.json을 만든 뒤 다시 실행하세요.")
        answer = match.structured_to_assignments(job, json.loads(answer_file.read_text()))
    else:
        if not opts.ai_key:
            raise PaintError("AI 매칭에 쓸 API 키가 없습니다. 설정의 AI 연결에 키를 넣으세요.")
        say(f"AI 매칭 중 (영역 {len(cand)}개)", 0.4)
        try:
            # 장면 이해(인물·부위) → 인물별 확대 검토 → 인물×부위 색을 차례로 묻는다
            raw = match.ask_structured(job, opts.ai_provider, opts.ai_key, opts.ai_model, zoom=opts.ai_zoom, check=opts.ai_check,
                                       say=lambda msg: say(msg, 0.45))
            usage = dict(job.usage)
            answer = match.structured_to_assignments(job, raw)
            if opts.ai_review:
                # 칠한 결과를 다시 보여 주고 틀린 번호만 고치게 한다 (요청 1회 추가)
                say("AI 검수 중", 0.5)
                rjob = match.review_job(job, labels, boundary, answer)
                answer = match.merge_review(answer, match.ask(rjob, opts.ai_provider, opts.ai_key, opts.ai_model))
                usage = {"model": usage["model"], "input": usage["input"] + rjob.usage["input"],
                         "output": usage["output"] + rjob.usage["output"], "calls": usage["calls"] + 1,
                         "fixed": answer["fixed"]}
            job.usage = usage
        except (RuntimeError, ValueError) as e:
            raise PaintError(f"AI 매칭 실패: {e}") from None
        # 무엇을 틀렸는지 보고 고칠 수 있게 요청과 답을 남긴다.
        # answer.json을 고친 뒤 'AI 매칭: 요청 폴더' 방식으로 다시 실행하면 고친 답을 쓴다
        folder = Path(out_path).with_name(Path(out_path).stem + "_AI매칭")
        match.save_job(job, folder)
        for i, im in enumerate(job.check_images, 1):
            im.save(folder / f"check_{i}.png")
        (folder / "answer.json").write_text(json.dumps(answer, ensure_ascii=False, indent=1))
    answer = match.fill_group_gaps(job, answer)
    # 칠한 결과 지도(번호 + 칠한 색)를 남긴다. 사람이나 MCP 클라이언트의 AI가 보고 검수한다
    folder = Path(out_path).with_name(Path(out_path).stem + "_AI매칭")
    folder.mkdir(parents=True, exist_ok=True)
    for old in folder.glob("result_*.png"):
        old.unlink()
    for i, im in enumerate(match.review_job(job, labels, boundary, answer).map_images, 1):
        im.convert("RGB").save(folder / f"result_{i}.png")
    region_key, keys = match.apply_answer(job, answer, n)
    names = list(dict.fromkeys(name for name, _ in keys))
    return region_key, keys, names, job.usage


def _ask_or_wait(job, save, opts, folder, say, msg):
    """요청 폴더 방식이면 답을 기다리고(없으면 요청을 남기고 멈춘다), API 방식이면 바로 묻는다. → 답, 사용량"""
    from . import match

    answer_file = Path(folder) / "answer.json"
    if opts.ai_mode == "files":
        if not answer_file.exists():
            save()
            raise AwaitingAnswer(f"{msg} 요청을 만들었습니다: {folder}. "
                                 "Claude Code·Codex에서 이 폴더의 README.md대로 answer.json을 만든 뒤 다시 실행하세요.")
        return json.loads(answer_file.read_text()), {}
    if not opts.ai_key:
        raise PaintError("AI의 API 키가 없습니다. 설정의 AI 연결에 키를 넣으세요.")
    say(f"AI가 {msg} 중", 0.45)
    answer = match.ask(job, opts.ai_provider, opts.ai_key, opts.ai_model)
    save()
    answer_file.write_text(json.dumps(answer, ensure_ascii=False, indent=1))
    return answer, dict(job.usage)


def split_leaks(labels, boundary, opts):
    """선이 크게 열려 한 영역으로 이어진 큰 덩어리(옆얼굴과 컷 배경 등)를 틈을 더 닫아 다시 나눈다.

    전체를 크게 닫으면 눈·가는 머리카락 같은 작은 영역이 선에 묻히므로, leak_min_area보다 넓은 영역만 나눈다.
    labels를 고친다. → 새 영역 수
    """
    n = int(labels.max())
    if opts.leak_gap_px <= 0:
        return n
    heavy, _ = ndimage.label(~(cv2.dilate(boundary.astype(np.uint8), _disk(1), iterations=opts.leak_gap_px) > 0))
    areas = np.bincount(labels.ravel(), minlength=n + 1)
    boxes = ndimage.find_objects(labels)
    for r in np.nonzero(areas >= opts.leak_min_area)[0]:
        if r == 0 or boxes[r - 1] is None:
            continue
        sl = boxes[r - 1]
        m = labels[sl] == r
        h = np.where(m, heavy[sl], 0)
        ids, cnt = np.unique(h[h > 0], return_counts=True)
        keep = ids[cnt >= opts.tile_min_area]
        if len(keep) < 2:
            continue
        # 크게 닫느라 선이 된 픽셀은 가장 가까운 덩어리를 따른다
        seed = np.isin(h, keep)
        iy, ix = ndimage.distance_transform_edt(~seed, return_indices=True)[1]
        full = np.where(m, h[iy, ix], 0)
        biggest = keep[np.argmax(cnt[np.isin(ids, keep)])]
        for k in keep:
            if k != biggest:
                n += 1
                labels[sl][full == k] = n
    return n


def _tiles_match(labels, n, boundary, frame, cand, chars, opts, folder, say):
    """영역 확인 칸: 글 AI가 영역마다 부위·누구인지 답한다 (그림 모델 없이)."""
    from . import characters

    n0, n = n, split_leaks(labels, boundary, opts)
    cand = np.r_[cand, np.arange(n0 + 1, n + 1)]  # 나눠서 새로 생긴 영역
    areas = np.bincount(labels.ravel(), minlength=n + 1)
    cand = cand[areas[cand] >= opts.tile_min_area]
    auto_bg = characters.panel_backgrounds(labels, frame) if opts.tile_auto_bg else set()
    regions = characters.tile_regions(labels, [r for r in cand if r not in auto_bg])
    say(f"영역 확인 칸 만드는 중 ({len(regions)}곳)", 0.4)
    sheets = characters.region_sheets(labels, boundary, regions)
    job, profiles = characters.tiles_job(sheets, chars, len(regions))
    answer, usage = _ask_or_wait(job, lambda: characters.save_tiles(job, profiles, regions, folder), opts, folder,
                                 say, "영역 확인")
    part_of, person_of, who, background = characters.from_tiles(answer, regions, n)
    # 한 사람 몸 안에서 몇 칸만 다른 이름으로 답한 실수를 이웃 다수로 바로잡는다
    characters.smooth_people(labels, person_of, part_of, adjacency(labels))
    background[list(auto_bg)] = True
    return part_of, person_of, who, background, usage, n


def _image_match(labels, boundary, frame, cand, bg, chars, opts, folder, say):
    """이미지 지도: 이미지 모델이 부위·사람을 칠하고, 글 AI가 누구인지 고른다."""
    from . import characters

    part_px, person_px, person_ids, chunks, usage = characters.image_maps(
        labels, boundary, np.isin(labels, cand[~bg[cand]]) | frame, opts.ai_image_key, opts.ai_image_model, folder,
        lambda msg: say(msg, 0.4))
    n = characters.split_mixed(labels, part_px, cand)
    bg_hint = np.r_[bg, np.zeros(n + 1 - len(bg), bool)]
    part_of, person_of, background = characters.decide(labels, n, part_px, person_px, person_ids, bg_hint)
    crops = characters.person_crops(labels, boundary, person_px, person_ids, chunks)
    who = {}
    if crops and opts.ai_mode != "off":
        job = characters.identify_job(crops, chars)
        answer, used = _ask_or_wait(job, lambda: characters.save_identify(job, crops, folder), opts, folder, say,
                                    "인물 알아보기")
        if used:
            usage = {"model": f"{usage['model']} + {used.get('model', opts.ai_model)}",
                     "input": usage["input"] + used.get("input", 0), "output": usage["output"] + used.get("output", 0),
                     "calls": usage["calls"] + 1}
        who = {p["person"]: p["character"] for p in answer.get("people", [])}
    return part_of, person_of, who, background, usage, n


def _answer_fits(folder, parts):
    """요청 폴더의 answer.json이 이 캐릭터 방식(칸·이미지 지도)의 요청에 대한 답인가."""
    try:
        answer = json.loads((folder / "answer.json").read_text())
    except (OSError, ValueError):
        return False
    if parts == "tiles":
        return (folder / "tiles.json").exists() and isinstance(answer.get("items"), list)
    return (folder / "characters.json").exists() and isinstance(answer.get("people"), list)


def character_match(labels, n, boundary, frame, opts, out_path, say):
    """캐릭터 프리셋으로 칠한다: 부위·누구인지는 AI가 정하고(opts.ai_parts), 색은 프리셋에서 가져온다.

    labels를 고칠 수 있다 (이미지 지도 방식에서 한 영역에 부위가 섞이면 나눈다).
    → (영역별 키, 키 목록, 레이어 이름 순서, 사용량, 비워 둘 배경 영역)
    """
    from . import characters

    chars = [c for c in opts.characters if c.get("colors")]
    if not chars:
        raise PaintError("캐릭터 프리셋이 없습니다. 색상 프리셋에 '캐릭터/부위' 이름으로 색을 넣으세요 (예: 하늘/머리).")
    areas = np.bincount(labels.ravel(), minlength=n + 1)
    bg = find_background(labels, n, frame, np.full(n + 1, -1, np.int64), opts)
    cand = np.nonzero(areas >= opts.small_area)[0]  # 배경도 보낸다: 컷을 통째로 봐야 실루엣을 안다
    cand = cand[cand > 0]
    folder = Path(out_path).with_name(Path(out_path).stem + "_AI매칭")
    answer_file = folder / "answer.json"
    if answer_file.exists() and not _answer_fits(folder, opts.ai_parts):
        # 다른 방식(예전 레퍼런스 매칭 등)이 남긴 답이면 이번 요청의 답이 아니다. 지우고 새로 묻는다
        answer_file.unlink()
    if folder.exists() and not (opts.ai_mode == "files" and answer_file.exists()):
        # 새로 묻는다: 전에 만든 지도·답은 지운다 (요청 폴더 방식에서 답을 받은 뒤 다시 실행할 때만 그대로 쓴다)
        for old in [*folder.glob("parts_*.png"), *folder.glob("persons_*.png"), *folder.glob("person_*.png"),
                    *folder.glob("tiles_*.png"), *folder.glob("profile_*.png"), folder / "tiles.json",
                    folder / "characters.json", answer_file]:
            old.unlink(missing_ok=True)
    if opts.ai_parts == "tiles" and opts.ai_mode == "off":
        raise PaintError("영역 확인 칸 방식은 AI가 답해야 합니다. 설정의 AI 매칭을 켜세요 (MCP는 자동).")
    try:
        if opts.ai_parts == "tiles":
            part_of, person_of, who, background, usage, n = _tiles_match(labels, n, boundary, frame, cand, chars,
                                                                          opts, folder, say)
        else:
            part_of, person_of, who, background, usage, n = _image_match(labels, boundary, frame, cand, bg, chars,
                                                                           opts, folder, say)
    except RuntimeError as e:
        raise PaintError(f"캐릭터 매칭 실패: {e}") from None
    region_key, keys, names = characters.assign(part_of, person_of, who, chars, n, labels, background)
    return region_key, keys, names, usage, background


def paint(psd_path, out_path, opts=None, progress=None):
    """psd_path에 힌트대로 밑색을 넣어 out_path로 저장한다. progress(단계 설명, 0~1)를 부른다."""
    opts = opts or Options()
    say = progress or (lambda msg, frac: None)
    say("PSD 여는 중", 0.0)
    if Path(out_path).resolve() == Path(psd_path).resolve() and not backup_path(psd_path).exists():
        # 원본에 덮어쓰기: 처음 한 번만 원본을 '이름_백업.psd'로 남긴다
        shutil.copy2(psd_path, backup_path(psd_path))
    psd = open_psd(psd_path)
    say("선화 영역 나누는 중", 0.15)
    line_group, boundary, frame, labels, n = segment(psd, opts)
    say("참고 레이어 읽는 중", 0.3)
    ai_usage = {}
    keep_empty = None  # 캐릭터 방식에서 비워 둘 배경 영역
    if opts.hint_source == "characters":
        region_key, keys, names, ai_usage, keep_empty = character_match(labels, n, boundary, frame, opts, out_path, say)
        n = len(region_key) - 1
        image_mode = False
    else:
        try:
            key_map, keys, names, image_mode = read_hints(psd, opts, psd_path, boundary)
            say("영역에 힌트 대응하는 중", 0.4)
            region_key = assign_hints(labels, n, key_map, opts, dense=image_mode or opts.hint_source == "psd")
        except LineMismatch as e:
            if opts.ai_mode == "off":
                raise PaintError(f"{e}. 다른 페이지 레퍼런스로 칠하려면 설정에서 AI 매칭을 켜세요.") from None
            region_key, keys, names, ai_usage = ai_match(e, labels, n, boundary, frame, opts, out_path, say)
            image_mode = False
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
        if keep_empty is not None:
            bg |= keep_empty
        auto = auto_color(labels, n, region_key, bg, keys, opts)
        if auto:
            names.append(opts.auto_layer)
    names[:] = list(dict.fromkeys(names))
    merge_small(labels, n, region_key, opts)
    if keep_empty is not None:
        region_key[keep_empty] = -1  # 작은 배경 조각이 둘레를 따라 칠해지지 않게
    areas = np.bincount(labels.ravel(), minlength=n + 1)
    unhinted_large = int(((region_key < 0) & (areas >= opts.small_area))[1:].sum())
    if not (region_key >= 0).any():
        raise PaintError("칠할 영역이 없습니다 (참고 힌트도 자동 칠하기도 없음).")
    state = {"psd_path": str(psd_path), "out_path": str(out_path), "opts": opts, "labels": labels,
             "boundary": boundary, "region_key": region_key.copy(), "keys": list(keys), "names": list(names)}
    say("밑색 채우는 중", 0.6)
    cls = fill(labels, boundary, region_key, keys, opts)
    merged = cap_layers(cls, keys, names, opts)
    say("PSD 저장 중", 0.85)
    layers = write(psd, line_group, cls, keys, names, opts, out_path)
    preview = make_preview(cls, boundary, keys)
    say("완료", 1.0)
    return Result(Path(out_path), layers, n, hinted, unhinted_large, auto, merged, preview, ai_usage, state)


def next_options(opts, result):
    """연속 모드: 레퍼런스(이미지·PSD)로 칠한 파일의 결과를 다음 파일의 레퍼런스로 쓴다.

    웹툰은 같은 장면이 여러 페이지 이어지므로, 다른 회차 레퍼런스보다 직전 페이지 결과가 옷·인물을 맞추기 쉽다.
    파일 안 참고 레이어 방식이면 파일마다 힌트가 따로 있으므로 그대로 둔다.
    """
    from dataclasses import replace

    if opts.hint_source not in ("image", "psd") or opts.ai_mode == "off" or result is None:
        return opts
    return replace(opts, hint_source="psd", hint_path=str(result.out_path))


def quick_preview(state, width=PREVIEW_WIDTH):
    """선 아래 넓히기 없이 영역 색만 칠한 미리보기 (클릭 수정 중 바로 보여 줄 때). 선이 틈을 가린다."""
    lut = np.array([(255, 255, 255)] + [rgb for _, rgb in state["keys"]], np.uint8)
    rgb = lut[(state["region_key"] + 1)[state["labels"]]]
    rgb[state["boundary"]] = 0
    im = Image.fromarray(rgb)
    return im.resize((width, max(1, round(im.height * width / im.width))), Image.LANCZOS)


def region_at(state, x, y, search=8):
    """캔버스 좌표의 영역 번호. 선 위를 누르면 search px 안의 가장 가까운 영역. 없으면 0."""
    labels = state["labels"]
    h, w = labels.shape
    x, y = int(np.clip(x, 0, w - 1)), int(np.clip(y, 0, h - 1))
    if labels[y, x]:
        return int(labels[y, x])
    y0, y1, x0, x1 = max(0, y - search), min(h, y + search + 1), max(0, x - search), min(w, x + search + 1)
    ys, xs = np.nonzero(labels[y0:y1, x0:x1])
    if not ys.size:
        return 0
    i = np.argmin((ys + y0 - y) ** 2 + (xs + x0 - x) ** 2)
    return int(labels[ys[i] + y0, xs[i] + x0])


def set_region(state, region, layer, rgb):
    """영역 하나를 (레이어, 색)으로 바꾼다. layer가 None이면 비운다."""
    if layer is None:
        state["region_key"][region] = -1
        return
    key = (layer, tuple(int(v) for v in rgb))
    if key not in state["keys"]:
        state["keys"].append(key)
        if layer not in state["names"]:
            state["names"].insert(0, layer)
    state["region_key"][region] = state["keys"].index(key)


def repaint(state, progress=None):
    """클릭 수정한 상태로 밑색을 다시 채워 PSD를 다시 저장한다. AI는 부르지 않는다."""
    say = progress or (lambda msg, frac: None)
    opts = state["opts"]
    say("PSD 여는 중", 0.1)
    psd = PSDImage.open(state["psd_path"])
    line_group = line_groups(psd, opts)[0]
    keys, names = list(state["keys"]), list(state["names"])
    say("밑색 채우는 중", 0.4)
    cls = fill(state["labels"], state["boundary"], state["region_key"].copy(), keys, opts)
    merged = cap_layers(cls, keys, names, opts)
    say("PSD 저장 중", 0.8)
    layers = write(psd, line_group, cls, keys, names, opts, state["out_path"])
    preview = make_preview(cls, state["boundary"], keys)
    say("완료", 1.0)
    n = int(state["labels"].max())
    return Result(Path(state["out_path"]), layers, n, 0, 0, 0, merged, preview, {}, state)
