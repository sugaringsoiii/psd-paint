"""MCP로 psd-paint를 쓰는 AI 앱(Claude 데스크톱·Codex·Gemini CLI): 연결(등록)하고, 칠할 요청을 넣어 연다.

- Claude 데스크톱: claude_desktop_config.json 의 mcpServers. 새 대화 링크 claude://claude.ai/new?q=…
- Codex(ChatGPT 앱·CLI): ~/.codex/config.toml 의 [mcp_servers.psd-paint]. 새 스레드 링크 codex://new?prompt=…&path=…
- Gemini CLI: ~/.gemini/settings.json 의 mcpServers. 터미널에서 gemini -i "…" 로 연다.
링크는 요청을 입력창에 넣기만 하고 보내지 않는다 (사용자가 확인하고 보낸다).
"""
import json
import os
import re
import shlex
import shutil
import subprocess
import sys
import urllib.parse
from pathlib import Path

SERVER = "psd-paint"
CLIENTS = {"claude": "Claude 데스크톱", "codex": "Codex (ChatGPT 앱)", "gemini": "Gemini CLI"}


def server_command():
    """MCP 서버를 실행하는 명령. → (command, args, env)"""
    if getattr(sys, "frozen", False):  # 설치한 앱: 앱 실행 파일에 --mcp
        return sys.executable, ["--mcp"], {}
    root = str(Path(__file__).resolve().parents[1])
    return sys.executable, ["-m", "psdpaint.mcp_server"], {"PYTHONPATH": root}


def _appdata():
    if sys.platform == "darwin":
        return Path.home() / "Library" / "Application Support"
    if os.name == "nt":
        return Path(os.environ.get("APPDATA") or Path.home())
    return Path.home() / ".config"


def config_path(client):
    return {"claude": _appdata() / "Claude" / "claude_desktop_config.json",
            "codex": Path.home() / ".codex" / "config.toml",
            "gemini": Path.home() / ".gemini" / "settings.json"}[client]


def installed(client):
    if client == "claude":
        return Path("/Applications/Claude.app").exists() if sys.platform == "darwin" else config_path("claude").parent.exists()
    if client == "codex":
        return Path("/Applications/ChatGPT.app").exists() or bool(shutil.which("codex")) or config_path("codex").exists()
    return bool(shutil.which("gemini"))


def _read_json(path):
    try:
        return json.loads(path.read_text())
    except (OSError, ValueError):
        return {}


def _toml_block(command, args, env):
    q = lambda s: json.dumps(s, ensure_ascii=False)  # TOML 기본 문자열은 JSON 문자열과 같은 이스케이프를 쓴다
    lines = [f"[mcp_servers.{SERVER}]", f"command = {q(command)}", f"args = [{', '.join(q(a) for a in args)}]",
             "startup_timeout_sec = 60"]
    if env:
        lines += ["", f"[mcp_servers.{SERVER}.env]"] + [f"{k} = {q(v)}" for k, v in env.items()]
    return "\n".join(lines) + "\n"


def _without_toml_server(text):
    """config.toml 에서 [mcp_servers.psd-paint] 와 그 하위 표를 뺀다."""
    out, skip = [], False
    for line in text.splitlines(keepends=True):
        head = re.match(r"\s*\[([^\]]+)\]", line)
        if head:
            name = head.group(1).strip()
            skip = name == f"mcp_servers.{SERVER}" or name.startswith(f"mcp_servers.{SERVER}.")
        if not skip:
            out.append(line)
    return "".join(out)


def registered(client):
    """psd-paint가 이 앱에 지금 실행 명령 그대로 등록돼 있나."""
    command, args, _ = server_command()
    path = config_path(client)
    if client == "codex":
        try:
            text = path.read_text()
        except OSError:
            return False
        return f"[mcp_servers.{SERVER}]" in text and json.dumps(command, ensure_ascii=False) in text
    entry = _read_json(path).get("mcpServers", {}).get(SERVER, {})
    return entry.get("command") == command and entry.get("args") == args


def register(client):
    """psd-paint를 이 앱의 MCP 서버로 등록한다 (이미 있으면 지금 실행 명령으로 바꾼다). 원래 파일은 .bak 으로 남긴다."""
    command, args, env = server_command()
    path = config_path(client)
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        shutil.copyfile(path, path.with_name(path.name + ".bak"))
    if client == "codex":
        text = _without_toml_server(path.read_text() if path.exists() else "").rstrip()
        path.write_text((text + "\n\n" if text else "") + _toml_block(command, args, env))
        return
    data = _read_json(path)
    entry = {"command": command, "args": args}
    if env:
        entry["env"] = env
    data.setdefault("mcpServers", {})[SERVER] = entry
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2))


def prompt_for(files):
    """칠할 파일 목록을 넣은 요청 글."""
    lines = "\n".join(f"- {f}" for f in files)
    return ("psd-paint로 아래 PSD를 캐릭터 프리셋으로 밑색 칠해 줘. 파일마다 paint_psd(psd_path=…, characters=true)를 부르고, "
            "돌려준 영역 확인 칸을 보고 apply_characters로 답해 줘.\n" + lines)


def launch(client, prompt, folder=None):
    """요청을 입력창에 넣어 그 앱을 연다 (보내지는 않는다)."""
    if client == "claude":
        _open_url("claude://claude.ai/new?q=" + urllib.parse.quote(prompt))
    elif client == "codex":
        url = "codex://new?prompt=" + urllib.parse.quote(prompt)
        if folder:
            url += "&path=" + urllib.parse.quote(str(folder))
        _open_url(url)
    else:
        cmd = f"gemini -i {shlex.quote(prompt)}"
        if folder:
            cmd = f"cd {shlex.quote(str(folder))} && {cmd}"
        if sys.platform == "darwin":
            script = f'tell application "Terminal" to do script {json.dumps(cmd)}'
            subprocess.Popen(["osascript", "-e", script, "-e", 'tell application "Terminal" to activate'])
        elif os.name == "nt":
            subprocess.Popen(["cmd", "/c", "start", "cmd", "/k", "gemini", "-i", prompt], cwd=folder or None)
        else:
            subprocess.Popen(["x-terminal-emulator", "-e", "bash", "-lc", cmd])


def _open_url(url):
    if sys.platform == "darwin":
        subprocess.Popen(["open", url])
    elif os.name == "nt":
        os.startfile(url)  # noqa: S606 (사용자가 누른 버튼으로 연다)
    else:
        subprocess.Popen(["xdg-open", url])
