"""psd-paint MCP 서버: Claude 데스크톱·Claude Code 같은 MCP 클라이언트에서 밑색을 칠한다.

다른 페이지 레퍼런스는 API 키 없이 클라이언트의 AI(구독)가 직접 영역을 맞춘다.
paint_psd가 번호 지도와 레퍼런스 이미지를 돌려주면, AI가 영역마다 팔레트를 골라 apply_match로 넘긴다.

실행: python -m psdpaint.mcp_server   (stdio)
"""
import io
import json
import threading
from dataclasses import asdict
from pathlib import Path

from mcp.server.mcpserver import Image, MCPServer

from .cli import output_path
from .core import AwaitingAnswer, Options, PaintError, paint

server = MCPServer(
    name="psd-paint",
    instructions=(
        "선화 PSD에 밑색 레이어를 자동으로 깔아 주는 도구다. 인물을 칠할 때는 paint_psd(characters=true)를 쓴다. "
        "paint_psd로 칠한다. 레퍼런스가 다른 페이지면 paint_psd가 레퍼런스(P번호 팔레트)와 원고 번호 지도 이미지를 돌려준다. "
        "그때는 이미지를 보고 먼저 인물마다 자세와 인체 구조를 따라 부위(머리카락·피부·겉옷 …)별 번호를 나눈 뒤, "
        "레퍼런스에서 같은 인물의 부위별 팔레트를 골라 apply_match(people=…)를 호출한다. "
        "판단이 서지 않는 영역과 배경은 none으로 둔다. 엑스트라도 피부는 피부 팔레트를 고른다. "
        "apply_match가 돌려준 칠한 결과 지도를 보고 틀린 곳이 있으면 한 번 고쳐 다시 호출한다. "
        "인물을 캐릭터 색으로 칠할 때는 paint_characters를 쓴다(paint_psd(characters=true)와 같다). "
        "등장인물 대표 그림과 영역 확인 칸(영역마다 빨갛게 강조)을 돌려주면, "
        "칸마다 부위와 누구인지 적어 apply_characters(job, items=[...])를 호출한다. "
        "(앱 설정에서 Gemini 이미지 지도를 고른 경우에는 인물 그림을 돌려주니 apply_characters(job, people=[...])로 답한다.)"
    ),
)


def _partial(folder):
    """판마다 받은 칸 답 (전부 모이기 전)."""
    try:
        return {int(it["tile"]): it for it in json.loads((folder / "partial.json").read_text())}
    except (OSError, ValueError):
        return {}


def _tile_sheet(folder, got):
    """아직 답하지 않은 칸이 든 판 한 장을 돌려준다. 판을 한 장씩 보고 답하게 해서 칸이 많아도 흐트러지지 않게 한다."""
    from .characters import TILE_COLS, TILE_ROWS

    info = json.loads((folder / "tiles.json").read_text())
    count, per = len(info["regions"]), TILE_COLS * TILE_ROWS
    todo = next((i for i in range(1, info["sheets"] + 1)
                 if any(t not in got for t in range((i - 1) * per + 1, min(i * per, count) + 1))), 1)
    first, last = (todo - 1) * per + 1, min(todo * per, count)
    readme = (folder / "README.md").read_text()
    prompt = readme.split("\n\n", 2)[2].split("답은 schema.json")[0].strip()
    prompt = prompt.replace(f"칸 번호 1 ~ {count}를 모두 적는다.", f"이번 판의 칸 번호 {first} ~ {last}를 모두 적는다.")
    out = [f"영역 확인 판 {todo}/{info['sheets']} (칸 {first}~{last}, 전체 {count}칸 중 {len(got)}칸 답함). 작업: {folder}\n"
           f"이 판의 칸 {first}~{last}만 보고 칸마다 부위·누구인지 적어 apply_characters(job=위 경로, items=[…])를 호출하세요. "
           "다음 판은 답하면 이어서 드립니다. 칸을 하나씩 둘레의 얼굴·머리띠·옷깃까지 확인하고, 같은 사람은 판이 바뀌어도 같은 이름으로 적으세요.\n"]
    for i, name in enumerate(info["profiles"], 1):
        out += [f"[등장인물 {name}]", Image(path=folder / f"profile_{i}.png")]
    out += [f"[영역 확인 {todo}]", Image(path=folder / f"tiles_{todo}.png"), prompt]
    return out


def _preview_images(result, width=720, max_h=1400):
    """칠한 결과 미리보기. PSD 옆에 PNG로 저장하고(어느 앱에서든 열어 볼 수 있게),
    대화에는 컷이 잘 보이게 세로로 나눠 넣는다 (긴 웹툰 페이지를 한 장으로 줄이면 알아보기 어렵다)."""
    im = result.preview.convert("RGB")
    path = Path(result.out_path).with_name(Path(result.out_path).stem + "_미리보기.png")
    try:
        im.save(path)
        note = (f"미리보기 그림: {path}\n![밑색 미리보기]({path.as_uri()})\n"
                "사용자에게 이 미리보기 그림을 보여 주세요 (PSD를 열지 않아도 결과를 볼 수 있게).")
    except OSError:
        note = ""
    if im.width > width:
        im = im.resize((width, round(im.height * width / im.width)))
    parts = []
    for y in range(0, im.height, max_h):
        piece = im.crop((0, y, im.width, min(im.height, y + max_h)))
        if piece.convert("L").getextrema()[0] < 250:  # 빈 여백 조각은 넣지 않는다
            buf = io.BytesIO()
            piece.save(buf, "PNG", optimize=True)
            parts.append(Image(data=buf.getvalue(), format="png"))
    return ([note] if note else []) + parts


def _check_step(folder, state):
    """칸 답으로 칠한 뒤 한 번 검수하게 한다: 칠한 결과 위에 칸 번호를 얹어 보여 주고, 틀린 칸만 다시 받는다."""
    from .characters import check_maps

    info = json.loads((folder / "tiles.json").read_text())
    answer = json.loads((folder / "answer.json").read_text())
    if answer.get("checked"):  # 이미 한 번 검수했으면 다시 묻지 않는다
        return []
    out = ["[검수] 칠한 결과 위에 칸 번호를 얹었습니다. 등장인물 대표 그림과 견주어 색이 틀린 칸만 "
           f"apply_characters(job={folder}, items=[…], checked=true)로 다시 보내세요. 예: 손·얼굴이 옷 색, "
           "한 사람의 옷 일부가 다른 사람 색, 재킷인데 셔츠 색. 고칠 칸이 없으면 apply_characters(job, items=[], checked=true)를 "
           "부르거나 그대로 끝내세요. 한 번만 확인합니다."]
    for i, im in enumerate(check_maps(state, info["regions"]), 1):
        path = folder / f"check_{i}.png"
        im.convert("RGB").save(path)
        out += [f"[칠한 결과 {i}]", Image(path=path)]
    return out


def _summary(result):
    lines = [f"저장: {result.out_path}", f"밑색 레이어: {', '.join(result.layers)}"]
    if result.auto:
        lines.append(f"자동(회색)으로 칠한 영역: {result.auto}개")
    if result.merged:
        lines.append(f"'기타'로 합친 레이어: {', '.join(result.merged)}")
    return "\n".join(lines)


def _job_folder(out_path):
    out_path = Path(out_path)
    return out_path.with_name(out_path.stem + "_AI매칭")


def _run(psd_path, out_path, opts):
    """칠하기를 실행한다. 다른 페이지 레퍼런스면 매칭 요청(이미지·지시문)을 돌려준다."""
    import time

    started = time.time() - 1
    try:
        result = paint(psd_path, out_path, opts)
    except AwaitingAnswer:
        folder = _job_folder(out_path)
        # API 키는 파일에 남기지 않는다 (다시 실행할 때 키체인에서 읽는다)
        options = {**asdict(opts), "ai_key": "", "ai_image_key": ""}
        (folder / "job.json").write_text(json.dumps(
            {"psd_path": str(psd_path), "out_path": str(out_path), "options": options}, ensure_ascii=False))
        readme = (folder / "README.md").read_text()
        if (folder / "tiles.json").exists():
            return _tile_sheet(folder, {})
        if (folder / "characters.json").exists():
            prompt = readme.split("\n\n", 2)[2].split("답은 schema.json")[0].strip()
            out = [f"인물이 누구인지 알아봐야 합니다. 작업: {folder}\n"
                   "아래 인물 그림마다 등장인물 중 누구인지 골라 apply_characters(job=위 경로, people=[...]) 를 호출하세요.\n"]
            for pid in json.loads((folder / "characters.json").read_text()):
                out += [f"[인물 {pid}]", Image(path=folder / f"person_{pid}.png")]
            out.append(prompt)
            return out
        prompt = readme.split("\n\n", 2)[2].split("답은 schema.json")[0].strip()
        out = [f"다른 페이지 레퍼런스라 AI 매칭이 필요합니다. 매칭 작업: {folder}\n"
               "아래 이미지를 보고 원고 번호마다 팔레트를 골라 apply_match(job=위 경로, assignments=[...]) 를 호출하세요.\n"]
        for p in sorted(folder.glob("ref_*.png")):
            out += [f"[레퍼런스 {p.stem}]", Image(path=p)]
        for p in sorted(folder.glob("map_*.png")):
            out += [f"[원고 지도 {p.stem}]", Image(path=p)]
        out.append(prompt)
        return out
    except PaintError as e:
        return f"실패: {e}"
    out = [_summary(result)] + _preview_images(result)
    folder = _job_folder(out_path)
    if (folder / "tiles.json").exists() and (folder / "answer.json").exists() and result.state:
        out += _check_step(folder, result.state)
    # 이번 실행이 만든 결과 지도만 (예전 실행이 남긴 그림을 이번 결과로 보이지 않게)
    results = sorted(p for p in folder.glob("result_*.png") if p.stat().st_mtime >= started) if folder.exists() else []
    if results:
        out.append("[칠한 결과 지도] 번호마다 칠한 색이다. 분홍색은 none(회색 자동)이다. 레퍼런스와 비교해 "
                   "틀린 번호(인물이 바뀐 색, 회색 피부, 덩어리 안 한 조각만 다른 색)가 있으면 그 번호만 "
                   "apply_match(job, assignments=[...])로 고친다. 고칠 곳이 없으면 끝낸다.")
        out += [Image(path=p) for p in results]
    return out


@server.tool()
def inspect_psd(psd_path: str) -> str:
    """PSD의 레이어 구조(폴더·레이어 이름, 보이기 여부)를 보여 준다. 선화 폴더·참고 폴더 이름을 확인할 때 쓴다."""
    from psd_tools import PSDImage

    psd = PSDImage.open(psd_path)
    lines = [f"{Path(psd_path).name}  {psd.width}×{psd.height}"]

    def walk(group, depth):
        for layer in reversed(list(group)):  # 위 레이어부터
            mark = "📁" if layer.is_group() else "▫️"
            vis = "" if layer.visible else " (숨김)"
            lines.append(f"{'  ' * depth}{mark} {layer.name}{vis}")
            if layer.is_group():
                walk(layer, depth + 1)
    walk(psd, 0)
    return "\n".join(lines)


@server.tool()
def paint_psd(psd_path: str, reference: str = "", output_dir: str = "", line_group: str = "선화",
              hint_group: str = "참고", max_layers: int = 10, offset_px: int = 2, auto_fill: bool = True,
              boundary_layers: str = "컷*, *테두리*", overwrite: bool = False, characters: bool = False,
              parts: str = ""):
    """선화 PSD에 밑색 레이어를 깔아 새 PSD(원본이름_밑색.psd)로 저장한다. overwrite가 아니면 원본은 바꾸지 않는다.

    - reference를 비우면 PSD 안의 '참고' 폴더(점·낙서 힌트)를 쓰고, 힌트가 없는 영역은 회색으로 자동 칠한다.
    - reference에 PNG·JPG·PSD 경로를 주면 그 레퍼런스의 색을 따른다. 다른 페이지(캐릭터 시트 등)면
      레퍼런스·원고 번호 지도 이미지를 돌려주니, 보고 나서 apply_match를 호출한다.
    - boundary_layers: 선화 외에 경계로 쓸 레이어 이름 패턴(쉼표 구분, * 사용).
    - overwrite: 새 파일 대신 원본 PSD에 저장한다. 처음 한 번 원본을 '이름_백업.psd'로 남긴다.
    - characters: 캐릭터 프리셋 방식. psd-paint 앱에 저장한 색상 프리셋('캐릭터/부위')과 캐릭터 프로필(대표 그림·생김새)을 쓴다.
      등장인물 그림과 영역 확인 칸을 돌려주면 칸마다 부위·누구인지 apply_characters(items=…)로 알려 준다.
    - parts: 부위 나누기. tiles(영역 확인 칸, 기본 — Claude만으로 된다) · image(Gemini 이미지 지도, 앱에 Gemini 키 필요)
    """
    opts = Options(line_group=line_group, hint_group=hint_group, max_layers=max_layers, offset_px=offset_px,
                   auto_fill=auto_fill, ai_mode="files",
                   extra_boundary=[p.strip() for p in boundary_layers.split(",") if p.strip()])
    if characters:
        from PySide6.QtCore import QSettings

        from . import ai
        from . import characters as chars
        settings = QSettings("psd-paint", "psd-paint")
        opts.hint_source, opts.characters = "characters", chars.from_app_settings()
        opts.ai_parts = parts or settings.value("parts_method", "tiles")
        opts.tile_min_area = int(settings.value("tile_min_area", opts.tile_min_area))
        opts.tile_auto_bg = str(settings.value("tile_auto_bg", True)).lower() not in ("false", "0")
        opts.ai_image_model = settings.value("image_model", chars.IMAGE_MODEL)
        opts.ai_image_key = ai.get_key("gemini") if opts.ai_parts == "image" else ""
    elif reference:
        opts.hint_source = "psd" if reference.lower().endswith(".psd") else "image"
        opts.hint_path = reference
    out_path = Path(psd_path) if overwrite else output_path(psd_path, output_dir or None, "_밑색")
    out_path.parent.mkdir(parents=True, exist_ok=True)
    if characters:
        # 새로 칠하기다: 전에 이 파일을 칠할 때 남은 답은 쓰지 않는다 (답은 apply_characters로만 받는다)
        for name in ("answer.json", "partial.json"):
            (_job_folder(out_path) / name).unlink(missing_ok=True)
    return _run(psd_path, out_path, opts)


@server.tool()
def apply_match(job: str, people: list[dict] | None = None, objects: list[dict] | None = None,
                background: list[int] | None = None, assignments: list[dict] | None = None):
    """paint_psd가 돌려준 매칭 작업에 AI의 답을 적용해 칠한다.

    - job: paint_psd가 알려 준 매칭 작업 폴더 경로
    - people: 원고 인물마다 {"id", "description", "pose", "reference_character",
              "parts": [{"part": 부위 이름, "regions": [번호…], "palette": "P3" 또는 "none"}]}
      같은 인물의 같은 부위는 같은 색으로 칠한다. 피부를 none으로 두면 피부색으로 칠한다.
    - objects: [{"name": 사물 이름, "regions": [번호…]}] (회색으로 자동 칠함), background: 배경 번호 목록 (칠하지 않음)
    - assignments: 결과를 고칠 때만. [{"region": 번호, "palette": "P3" 또는 "none"}] 이전 답에서 이 번호들만 바꾼다
    """
    folder = Path(job)
    info = json.loads((folder / "job.json").read_text())
    answer_file = folder / "answer.json"
    if people is not None:
        answer = {"people": people, "objects": objects or [], "background": background or []}
        if assignments:
            answer["corrections"] = assignments
    elif assignments and answer_file.exists():
        answer = json.loads(answer_file.read_text())
        answer["corrections"] = answer.get("corrections", []) + assignments
    else:
        answer = {"assignments": assignments or []}
    answer_file.write_text(json.dumps(answer, ensure_ascii=False, indent=1))
    opts = Options(**info["options"])
    return _run(info["psd_path"], info["out_path"], opts)


@server.tool()
def paint_characters(psd_path: str, output_dir: str = "", overwrite: bool = False, parts: str = ""):
    """psd-paint 앱에 등록한 캐릭터(대표 그림·생김새·'캐릭터/부위' 색)로 인물 밑색을 칠한다.

    등장인물 대표 그림과 영역 확인 판(칸 20개)을 한 장씩 돌려주니, 그 판의 칸마다 부위·누구인지 적어
    apply_characters(job, items=[…])를 호출한다. 다음 판이 오면 같은 방식으로 이어서 답한다.
    결과는 원본이름_밑색.psd (overwrite면 원본)에 저장한다. paint_psd(characters=true)와 같다.
    """
    return paint_psd(psd_path, output_dir=output_dir, overwrite=overwrite, characters=True, parts=parts)


@server.tool()
def apply_characters(job: str, items: list[dict] | None = None, people: list[dict] | None = None,
                     checked: bool = False):
    """paint_psd(characters=true)의 요청에 답하고 칠한다.

    - job: paint_psd가 알려 준 작업 폴더 경로
    - items: 영역 확인 칸의 답. [{"tile": 칸 번호, "part": 부위·사물·배경, "character": 등장인물 이름·모름·없음}]
      판을 한 장씩 받는다: 받은 판의 칸만 답하면 다음 판을 돌려주고, 모든 칸이 모이면 칠한다.
      칠한 뒤 고칠 때는 바꿀 칸만 다시 보내도 된다 (전 답에 덮어쓴다).
    - checked: 칠한 결과를 보고 고친 답이면 true (검수 지도를 다시 보여 주지 않는다)
    - people: 이미지 지도 방식의 인물 답. [{"person": "1A", "character": 등장인물 이름 또는 "모름", "reason": 짧은 근거}]
    """
    folder = Path(job)
    info = json.loads((folder / "job.json").read_text())
    answer_file = folder / "answer.json"
    if items is not None and not answer_file.exists() and (folder / "tiles.json").exists():
        # 판마다 받는 중: 모든 칸이 모이면 칠하고, 아니면 다음 판을 준다
        got = _partial(folder)
        got.update({int(it["tile"]): it for it in items})
        count = len(json.loads((folder / "tiles.json").read_text())["regions"])
        if any(t not in got for t in range(1, count + 1)):
            (folder / "partial.json").write_text(json.dumps([got[k] for k in sorted(got)], ensure_ascii=False))
            return _tile_sheet(folder, got)
        items = list(got.values())
        (folder / "partial.json").unlink(missing_ok=True)
    if items is not None:
        old = json.loads(answer_file.read_text()).get("items", []) if answer_file.exists() else []
        merged = {int(it["tile"]): it for it in old}
        merged.update({int(it["tile"]): it for it in items})
        answer = {"items": [merged[k] for k in sorted(merged)], "checked": checked}
    else:
        answer = {"people": people or []}
    answer_file.write_text(json.dumps(answer, ensure_ascii=False, indent=1))
    from . import ai
    opts = Options(**info["options"])
    opts.ai_image_key = ai.get_key("gemini")
    return _run(info["psd_path"], info["out_path"], opts)


def main():
    # psd-tools·scipy는 import 사슬이 깊다. 도구가 작업 스레드에서 돌 때 스택이 넘치지 않게
    # 스레드 스택을 늘리고 무거운 모듈은 미리 불러 둔다
    threading.stack_size(64 * 1024 * 1024)
    import psd_tools.composite  # noqa: F401
    import scipy.ndimage  # noqa: F401

    server.run("stdio")


if __name__ == "__main__":
    main()
