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
        "선화 PSD에 밑색 레이어를 자동으로 깔아 주는 도구다. "
        "paint_psd로 칠한다. 레퍼런스가 다른 페이지면 paint_psd가 레퍼런스(P번호 팔레트)와 원고 번호 지도 이미지를 돌려준다. "
        "그때는 이미지를 보고 먼저 인물마다 자세와 인체 구조를 따라 부위(머리카락·피부·겉옷 …)별 번호를 나눈 뒤, "
        "레퍼런스에서 같은 인물의 부위별 팔레트를 골라 apply_match(people=…)를 호출한다. "
        "판단이 서지 않는 영역과 배경은 none으로 둔다. 엑스트라도 피부는 피부 팔레트를 고른다. "
        "apply_match가 돌려준 칠한 결과 지도를 보고 틀린 곳이 있으면 한 번 고쳐 다시 호출한다. "
        "paint_psd(characters=true)는 캐릭터 프리셋 방식이다. 이미지 모델이 부위·인물을 나눈 뒤 인물마다 잘라 낸 선화를 돌려주면, "
        "등장인물 목록에서 누구인지 골라 apply_characters를 호출한다."
    ),
)


def _png(im, width=480):
    if im.width > width:  # 대화에 넣는 미리보기는 작게
        im = im.resize((width, round(im.height * width / im.width)))
    buf = io.BytesIO()
    im.convert("RGB").save(buf, "PNG", optimize=True)
    return Image(data=buf.getvalue(), format="png")


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
    try:
        result = paint(psd_path, out_path, opts)
    except AwaitingAnswer:
        folder = _job_folder(out_path)
        # API 키는 파일에 남기지 않는다 (다시 실행할 때 키체인에서 읽는다)
        options = {**asdict(opts), "ai_key": "", "ai_image_key": ""}
        (folder / "job.json").write_text(json.dumps(
            {"psd_path": str(psd_path), "out_path": str(out_path), "options": options}, ensure_ascii=False))
        readme = (folder / "README.md").read_text()
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
    out = [_summary(result), _png(result.preview)]
    folder = _job_folder(out_path)
    results = sorted(folder.glob("result_*.png")) if folder.exists() else []
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
              boundary_layers: str = "컷*, *테두리*", overwrite: bool = False, characters: bool = False):
    """선화 PSD에 밑색 레이어를 깔아 새 PSD(원본이름_밑색.psd)로 저장한다. overwrite가 아니면 원본은 바꾸지 않는다.

    - reference를 비우면 PSD 안의 '참고' 폴더(점·낙서 힌트)를 쓰고, 힌트가 없는 영역은 회색으로 자동 칠한다.
    - reference에 PNG·JPG·PSD 경로를 주면 그 레퍼런스의 색을 따른다. 다른 페이지(캐릭터 시트 등)면
      레퍼런스·원고 번호 지도 이미지를 돌려주니, 보고 나서 apply_match를 호출한다.
    - boundary_layers: 선화 외에 경계로 쓸 레이어 이름 패턴(쉼표 구분, * 사용).
    - overwrite: 새 파일 대신 원본 PSD에 저장한다. 처음 한 번 원본을 '이름_백업.psd'로 남긴다.
    - characters: 캐릭터 프리셋 방식. psd-paint 앱에 저장한 색상 프리셋('캐릭터/부위')과 생김새를 쓴다.
      Gemini 이미지 모델이 부위·인물을 나누고(앱에 Gemini 키 필요), 인물 그림을 돌려주면 apply_characters로 누구인지 알려 준다.
    """
    opts = Options(line_group=line_group, hint_group=hint_group, max_layers=max_layers, offset_px=offset_px,
                   auto_fill=auto_fill, ai_mode="files",
                   extra_boundary=[p.strip() for p in boundary_layers.split(",") if p.strip()])
    if characters:
        from PySide6.QtCore import QSettings

        from . import ai
        from . import characters as chars
        opts.hint_source, opts.characters = "characters", chars.from_app_settings()
        opts.ai_image_model = QSettings("psd-paint", "psd-paint").value("image_model", chars.IMAGE_MODEL)
        opts.ai_image_key = ai.get_key("gemini")
    elif reference:
        opts.hint_source = "psd" if reference.lower().endswith(".psd") else "image"
        opts.hint_path = reference
    out_path = Path(psd_path) if overwrite else output_path(psd_path, output_dir or None, "_밑색")
    out_path.parent.mkdir(parents=True, exist_ok=True)
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
def apply_characters(job: str, people: list[dict]):
    """paint_psd(characters=true)가 돌려준 인물마다 누구인지 알려 주고 칠한다.

    - job: paint_psd가 알려 준 작업 폴더 경로
    - people: [{"person": "1A", "character": 등장인물 이름 또는 "모름", "reason": 짧은 근거}]
    """
    folder = Path(job)
    info = json.loads((folder / "job.json").read_text())
    (folder / "answer.json").write_text(json.dumps({"people": people}, ensure_ascii=False, indent=1))
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
