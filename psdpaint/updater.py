"""앱 안 업데이트: GitHub 최신 릴리스를 확인하고, 설치 파일을 받아 지금 앱과 바꾼 뒤 다시 켠다.

설치한 앱(PyInstaller 로 묶은 앱)에서만 동작한다. 소스로 실행하면 확인만 건너뛴다.
앱이 직접 받은 파일에는 macOS 의 '인터넷에서 받은 파일' 표시가 붙지 않아 Gatekeeper 경고가 뜨지 않는다.
"""
import json
import os
import shutil
import subprocess
import sys
import tempfile
import urllib.request
import zipfile
from pathlib import Path

from . import __version__

REPO = "sugaringsoiii/psd-paint"


class InstallFailed(Exception):
    """받기는 했지만 바꿔 끼우지 못했다. path 는 받은 설치 파일 (손으로 설치하게 열어 줄 수 있다)"""

    def __init__(self, path, reason):
        super().__init__(reason)
        self.path = path


def installed():
    """설치한 앱으로 실행 중인가 (소스 실행이면 False)"""
    return getattr(sys, "frozen", False)


def parse(v):
    """'v0.2.1' → (0, 2, 1)"""
    return tuple(int(x) for x in v.lstrip("v").split("."))


def asset_suffix():
    return "-windows.zip" if sys.platform == "win32" else "-mac-arm64.dmg"


def pick(release, current=__version__):
    """릴리스 정보(JSON)에서 더 새 버전의 이 OS 용 설치 파일을 고른다. → (버전, 받을 주소) 또는 None"""
    version = release.get("tag_name", "").lstrip("v")
    try:
        if parse(version) <= parse(current):
            return None
    except ValueError:
        return None
    for a in release.get("assets", []):
        if a["name"].endswith(asset_suffix()):
            return version, a["browser_download_url"]
    return None


def check():
    """새 버전이 있으면 (버전, 주소). 없거나 확인하지 못하면 None"""
    req = urllib.request.Request(f"https://api.github.com/repos/{REPO}/releases/latest",
                                 headers={"Accept": "application/vnd.github+json", "User-Agent": "psd-paint"})
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            return pick(json.load(r))
    except (OSError, ValueError):  # 인터넷 없음·요청 한도 초과 등은 조용히 넘어간다
        return None


def download(url, dest, progress=lambda frac: None):
    req = urllib.request.Request(url, headers={"User-Agent": "psd-paint"})
    with urllib.request.urlopen(req, timeout=30) as r, open(dest, "wb") as f:
        total = int(r.headers.get("Content-Length") or 0)
        done = 0
        while chunk := r.read(1 << 16):
            f.write(chunk)
            done += len(chunk)
            if total:
                progress(done / total)
    return dest


def current_app():
    """바꿔 끼울 대상: Mac 은 .app 묶음, Windows 는 실행 파일이 든 폴더"""
    exe = Path(sys.executable).resolve()
    return exe.parents[2] if sys.platform == "darwin" else exe.parent


def install_mac(dmg, target):
    """dmg 안의 .app 으로 target(.app)을 바꾼다. 실패하면 원래 앱을 그대로 둔다."""
    mount = Path(tempfile.mkdtemp(prefix="psd-paint-update-"))
    subprocess.run(["hdiutil", "attach", "-nobrowse", "-readonly", "-mountpoint", str(mount), str(dmg)],
                   check=True, capture_output=True)
    try:
        src = next(mount.glob("*.app"))
        new = target.with_name(target.name + ".new")
        old = target.with_name(target.name + ".old")
        shutil.rmtree(new, ignore_errors=True)
        shutil.rmtree(old, ignore_errors=True)
        subprocess.run(["ditto", str(src), str(new)], check=True)  # 서명·속성을 그대로 복사
        target.rename(old)  # 실행 중인 앱도 옮길 수 있다 (열린 파일은 그대로 남는다)
        try:
            new.rename(target)
        except OSError:
            old.rename(target)  # 되돌린다
            raise
        shutil.rmtree(old, ignore_errors=True)
    finally:
        subprocess.run(["hdiutil", "detach", "-quiet", str(mount)], check=False)
        shutil.rmtree(mount, ignore_errors=True)


def relaunch_mac(target):
    """이 앱이 끝난 뒤 새 앱을 연다."""
    subprocess.Popen(["/bin/sh", "-c", f'sleep 1; open "{target}"'], start_new_session=True)


def install_windows(zip_path, target):
    """앱이 끝나기를 기다렸다가 폴더를 바꾸고 다시 켜는 배치 파일을 띄운다. (실행 중인 exe 는 지울 수 없어서)"""
    work = Path(tempfile.mkdtemp(prefix="psd-paint-update-"))
    with zipfile.ZipFile(zip_path) as z:
        z.extractall(work)
    new = next(p for p in work.iterdir() if p.is_dir())
    exe = target / "psd-paint.exe"
    bat = work / "update.bat"
    bat.write_text(f"""@echo off
:wait
tasklist /FI "PID eq {os.getpid()}" | find "{os.getpid()}" >nul && (timeout /t 1 /nobreak >nul & goto wait)
robocopy "{new}" "{target}" /MIR /NFL /NDL /NJH /NJS /NP >nul
start "" "{exe}"
""", encoding="mbcs")
    flags = subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP
    subprocess.Popen(["cmd", "/c", str(bat)], creationflags=flags, close_fds=True)


def update(url, progress=lambda frac: None):
    """받아서 바꿔 끼우고 다시 켤 준비를 한다. 호출한 쪽은 끝나면 앱을 종료한다."""
    target = current_app()
    tmp = Path(tempfile.mkdtemp(prefix="psd-paint-dl-")) / url.rsplit("/", 1)[-1]
    download(url, tmp, progress)
    try:
        if sys.platform == "darwin":
            install_mac(tmp, target)
            relaunch_mac(target)
        else:
            install_windows(tmp, target)
    except (OSError, subprocess.CalledProcessError, StopIteration, zipfile.BadZipFile) as e:
        # macOS 가 앱 바꾸기를 막거나(앱 관리 권한) 응용 프로그램 폴더에 쓸 수 없는 경우
        raise InstallFailed(tmp, str(e) or type(e).__name__) from None


if __name__ == "__main__":  # 자체 점검: 버전 비교와 설치 파일 고르기
    rel = {"tag_name": "v9.0.0", "assets": [{"name": "psd-paint-9.0.0-mac-arm64.dmg", "browser_download_url": "m"},
                                            {"name": "psd-paint-9.0.0-windows.zip", "browser_download_url": "w"}]}
    assert pick(rel, "0.2.1") == ("9.0.0", "w" if sys.platform == "win32" else "m")
    assert pick(rel, "9.0.0") is None
    assert pick({"tag_name": "v0.10.0", "assets": rel["assets"]}, "0.9.9")  # 숫자로 비교 (문자열 비교면 틀린다)
    assert pick({"tag_name": "nightly", "assets": rel["assets"]}) is None
    print("ok")
