"""설치 파일을 만든다. macOS: dist/psd-paint-<버전>-mac-<arch>.dmg, Windows: dist/psd-paint-<버전>-windows.zip

usage: python build.py
"""
import platform
import shutil
import subprocess
import sys
from pathlib import Path

import PyInstaller.__main__

ROOT = Path(__file__).parent
sys.path.insert(0, str(ROOT))
from psdpaint import __version__  # noqa: E402

NAME = "psd-paint"
SEP = ";" if sys.platform == "win32" else ":"


def main():
    icon = ROOT / "psdpaint" / "assets" / ("icon.ico" if sys.platform == "win32" else "icon.icns")
    args = [
        str(ROOT / "app_main.py"),
        "--name", NAME,
        "--windowed",           # 콘솔 창 없이
        "--noconfirm",
        "--clean",
        "--icon", str(icon),
        "--add-data", f"{ROOT / 'psdpaint' / 'assets'}{SEP}psdpaint/assets",
        # 동적으로 불러와 PyInstaller가 못 찾는 모듈
        "--collect-submodules", "psd_tools",
        "--collect-submodules", "keyring.backends",
        "--hidden-import", "psdpaint.mcp_server",
        "--collect-all", "truststore",
        "--copy-metadata", "keyring",
        "--exclude-module", "tkinter",
        "--osx-bundle-identifier", "io.github.sugaringsoiii.psd-paint",
    ]
    PyInstaller.__main__.run(args)
    dist = ROOT / "dist"
    if sys.platform == "darwin":
        app = dist / f"{NAME}.app"
        plist = app / "Contents" / "Info.plist"
        subprocess.run(["plutil", "-replace", "CFBundleShortVersionString", "-string", __version__, str(plist)], check=True)
        subprocess.run(["plutil", "-replace", "CFBundleVersion", "-string", __version__, str(plist)], check=True)
        # 설치 화면: .app과 응용 프로그램 폴더 바로가기를 담은 디스크 이미지
        stage = dist / "dmg"
        shutil.rmtree(stage, ignore_errors=True)
        stage.mkdir()
        shutil.copytree(app, stage / app.name, symlinks=True)
        (stage / "Applications").symlink_to("/Applications")
        out = dist / f"{NAME}-{__version__}-mac-{platform.machine()}.dmg"
        out.unlink(missing_ok=True)
        subprocess.run(["hdiutil", "create", "-volname", NAME, "-srcfolder", str(stage), "-ov", "-format", "UDZO",
                        str(out)], check=True)
        shutil.rmtree(stage)
    else:
        out = dist / f"{NAME}-{__version__}-windows"
        shutil.make_archive(str(out), "zip", dist, NAME)
        out = out.with_suffix(".zip")
    print("built:", out)


if __name__ == "__main__":
    main()
