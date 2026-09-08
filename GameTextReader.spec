# -*- mode: python ; coding: utf-8 -*-
from PyInstaller.utils.hooks import collect_all
from PyInstaller.utils.hooks import collect_dynamic_libs
from PyInstaller.utils.hooks import collect_submodules
from PyInstaller.utils.win32.versioninfo import VSVersionInfo, FixedFileInfo, StringFileInfo, StringTable, StringStruct, VarFileInfo, VarStruct
import json
from pathlib import Path
import sys
sys.path.insert(0, SPECPATH)
from app_version import APP_VERSION, FILE_VERSION, source_build_info

metadata = source_build_info()
metadata_path = Path(workpath) / 'build_info.json'
metadata_path.parent.mkdir(parents=True, exist_ok=True)
metadata_path.write_text(json.dumps(metadata), encoding='utf-8')
version_resource = VSVersionInfo(
    ffi=FixedFileInfo(filevers=FILE_VERSION, prodvers=FILE_VERSION, mask=0x3f, flags=0, OS=0x40004, fileType=1, subtype=0, date=(0, 0)),
    kids=[StringFileInfo([StringTable('040904B0', [
        StringStruct('FileDescription', 'Game Text Reader'),
        StringStruct('FileVersion', APP_VERSION),
        StringStruct('ProductVersion', APP_VERSION),
        StringStruct('ProductName', 'Game Text Reader'),
        StringStruct('Comments', metadata['build']),
    ])]), VarFileInfo([VarStruct('Translation', [1033, 1200])])],
)


datas = [("assets", "assets")]
datas.append((str(metadata_path), '.'))
binaries = []
hiddenimports = []
binaries += collect_dynamic_libs("winrt")
hiddenimports += collect_submodules("winrt")
hiddenimports += collect_submodules("pystray")
tmp_ret = collect_all("winocr")
datas += tmp_ret[0]
binaries += tmp_ret[1]
hiddenimports += tmp_ret[2]
tmp_ret = collect_all("symspellpy")
datas += tmp_ret[0]
binaries += tmp_ret[1]
hiddenimports += tmp_ret[2]


a = Analysis(
    ["main.py"],
    pathex=[],
    binaries=binaries,
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[],
    noarchive=False,
    optimize=0,
)
pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="GameTextReader",
    icon="assets/app_icon.ico",
    version=version_resource,
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    manifest="assets/GameTextReader.manifest",
)
coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=True,
    upx_exclude=[],
    name="GameTextReader",
)
