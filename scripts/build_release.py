"""Build the unified client for Windows x64 or Apple Silicon from locked wheels."""
from __future__ import annotations

import argparse
import base64
import json
import os
import posixpath
import shutil
import struct
import subprocess
import sys
import tarfile
import urllib.request
import zipfile
from pathlib import Path, PurePosixPath

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from ournotes.platforms.system import SUPPORTED_PLATFORMS, current_platform
from ournotes.updates.manifest import canonical, file_hash, verify, version_tuple

PYTHON_VERSION = "3.12.10"
RUNTIME_SHA256 = {
    'windows-x64': '4acbed6dd1c744b0376e3b1cf57ce906f9dc9e95e68824584c8099a63025a3c3',
    'macos-arm64': '2579c9ffcbb0a745f1de3d645612c7466269eb9a23316a54ece49d50c1c9122f',
}
RUNTIMES = {
    "windows-x64": (f"python-{PYTHON_VERSION}-embed-amd64.zip",
                    f"https://www.python.org/ftp/python/{PYTHON_VERSION}/python-{PYTHON_VERSION}-embed-amd64.zip"),
    "macos-arm64": (f"cpython-{PYTHON_VERSION}+20250529-aarch64-apple-darwin-install_only_stripped.tar.gz",
                    f"https://github.com/astral-sh/python-build-standalone/releases/download/20250529/cpython-{PYTHON_VERSION}%2B20250529-aarch64-apple-darwin-install_only_stripped.tar.gz"),
}
SCRIPT_NAMES = {"webui.py", "maintain_storage.py"}
LOGIN_HISTORY_ASSETS = ("expand.png", "remove.png")
CLIENT_PACKAGES = {"accounts", "catalogs", "common", "control", "integrations", "platforms",
                   "proxy", "rebind", "reports", "runtime", "ui", "updates", "vision"}
BOOT_FILES = {"windows-x64": ("AGENTS.md", "启动控制台.cmd", "boot.ps1", "boot_client.py", "configure_client.py"),
              "macos-arm64": ("AGENTS.md", "启动控制台.command", "boot.command", "boot_client.py", "configure_client.py")}


def native_imports(path):
    """Read PE imports without loading DLLs into the developer process."""
    raw = path.read_bytes()
    if raw[:2] != b"MZ":
        return []
    pe = struct.unpack_from("<I", raw, 60)[0]
    optional = pe + 24
    sections = struct.unpack_from("<H", raw, pe + 6)[0]
    optional_size = struct.unpack_from("<H", raw, pe + 20)[0]
    directory = optional + (112 if struct.unpack_from("<H", raw, optional)[0] == 0x20B else 96)
    import_rva = struct.unpack_from("<I", raw, directory + 8)[0]
    if not import_rva:
        return []
    ranges = [struct.unpack_from("<IIII", raw, optional + optional_size + number * 40 + 8) for number in range(sections)]
    def offset(rva):
        return next((pointer + rva - address for size, address, raw_size, pointer in ranges if address <= rva < address + max(size, raw_size)), rva)
    cursor, result = offset(import_rva), []
    while any(raw[cursor:cursor + 20]):
        name = offset(struct.unpack_from("<I", raw, cursor + 12)[0])
        result.append(raw[name:raw.index(0, name)].decode("ascii"))
        cursor += 20
    return result


def include_vc_runtime(runtime, selected=None):
    """Only app-local Microsoft redistributable binaries, never System32 copies."""
    if selected is None:
        candidates = []
        for program in ("ProgramFiles", "ProgramFiles(x86)"):
            base = Path(os.environ.get(program, "C:/Program Files")) / "Microsoft Visual Studio"
            if base.is_dir():
                candidates.extend(base.glob("20*/*/VC/Redist/MSVC/*/x64/Microsoft.VC*.CRT"))
        if not candidates:
            raise ValueError("找不到官方VC CRT Redist目录，请用--vc-runtime指定x64 Microsoft.VC*.CRT；不会从System32复制。")
        selected = max(candidates, key=lambda path: tuple(int(part) for part in path.parents[1].name.split(".")))
    selected = Path(selected)
    if not all((selected / name).is_file() for name in ("msvcp140.dll", "msvcp140_1.dll", "vcruntime140.dll", "vcruntime140_1.dll")):
        raise ValueError("VC CRT Redist缺少必要DLL。")
    for path in selected.glob("*.dll"):
        shutil.copy2(path, runtime / path.name)
    return str(selected)


def verify_vc_imports(runtime):
    shipped = {path.name.casefold() for path in runtime.rglob("*") if path.is_file()}
    missing = set()
    for path in runtime.rglob("*"):
        if path.suffix.lower() in (".dll", ".exe", ".pyd"):
            for name in native_imports(path):
                if any(part in name.lower() for part in ("msvcp", "vcruntime", "concrt")) and name.casefold() not in shipped:
                    missing.add(name)
    if missing:
        raise ValueError("运行时缺少VC依赖：" + ", ".join(sorted(missing)))


def copy_tree(source, target):
    shutil.copytree(source, target, ignore=shutil.ignore_patterns("__pycache__", "*.pyc", "*.pyo"))


def stage_source(destination):
    destination.mkdir(parents=True)
    (destination / "ournotes").mkdir()
    for name in ("__init__.py", "paths.py"):
        shutil.copy2(ROOT / "ournotes" / name, destination / "ournotes" / name)
    for name in sorted(CLIENT_PACKAGES):
        copy_tree(ROOT / "ournotes" / name, destination / "ournotes" / name)
    # Setup-only browser profiles and offline charting do not belong in the client.
    for name in ('integrations/oauth_setup.py', 'integrations/browser.py', 'reports/statistics.py'):
        (destination / 'ournotes' / name).unlink(missing_ok=True)
    history_icons = destination / "ournotes/vision/assets/history-icons"
    if history_icons.is_dir():
        shutil.rmtree(history_icons)
    for path in (destination / "ournotes/vision/assets").glob("history-*.png"):
        path.unlink()
    for name in ("launcher", "account-browser"):
        copy_tree(ROOT / "web" / name, destination / "web" / name)
    (destination / "config").mkdir()
    for name in ("presets.json", "input.json", "storage-policy.json"):
        shutil.copy2(ROOT / "config" / name, destination / "config" / name)
    copy_tree(ROOT / "assets/ui", destination / "assets/ui")
    login_history = destination / "assets/login-history"
    login_history.mkdir(parents=True)
    for name in LOGIN_HISTORY_ASSETS:
        shutil.copy2(ROOT / "assets/login-history" / name, login_history / name)
    copy_tree(ROOT / "assets/vision-fallback", destination / "assets/vision-fallback")
    (destination / "scripts").mkdir(exist_ok=True)
    for name in SCRIPT_NAMES:
        source = ROOT / "scripts" / name
        if source.is_file():
            shutil.copy2(source, destination / "scripts" / name)


def extract_runtime(source, destination, platform):
    """Flatten only internal runtime links; never extract archive links to the host."""
    executables = set()
    if platform == "windows-x64":
        with zipfile.ZipFile(source) as archive:
            for item in archive.infolist():
                path = PurePosixPath(item.filename)
                if (path.is_absolute() or "\\" in item.filename or ":" in item.filename
                        or ".." in path.parts or item.external_attr >> 16 & 0o170000 == 0o120000):
                    raise ValueError("Python运行时包路径无效。")
            archive.extractall(destination)
        binary = destination / "python.exe"
        raw = binary.read_bytes()
        if raw[:2] != b'MZ' or struct.unpack_from('<H', raw, struct.unpack_from('<I', raw, 60)[0] + 4)[0] != 0x8664:
            raise ValueError('Windows运行时必须为x64 Python。')
        pth = next(destination.glob("python*._pth"))
        pth.write_text("python312.zip\n.\n../app\nLib/site-packages\nLib/site-packages/win32\nLib/site-packages/win32/lib\nLib/site-packages/Pythonwin\nimport site\n", "utf-8")
        return executables
    with tarfile.open(source, "r:gz") as archive:
        members = {}
        for item in archive.getmembers():
            path = PurePosixPath(item.name)
            if (path.is_absolute() or "\\" in item.name or ":" in item.name
                    or ".." in path.parts or not path.parts or path.parts[0] != 'python'):
                raise ValueError("macOS Python运行时包路径无效。")
            if not (item.isdir() or item.isfile() or item.issym() or item.islnk()):
                raise ValueError("macOS运行时包含非普通文件。")
            if item.name in members:
                raise ValueError("macOS运行时包含重复路径。")
            members[item.name] = item

        def read_member(name, visited=()):
            item = members.get(name)
            if item is None or name in visited:
                raise ValueError('macOS运行时包含缺失或循环链接。')
            if item.issym() or item.islnk():
                if PurePosixPath(item.linkname).is_absolute():
                    raise ValueError('macOS运行时链接越界。')
                target = posixpath.normpath(posixpath.join(posixpath.dirname(name), item.linkname)
                                           if item.issym() else item.linkname)
                if not target.startswith('python/'):
                    raise ValueError('macOS运行时链接越界。')
                return read_member(target, (*visited, name))
            if not item.isfile():
                raise ValueError('macOS运行时链接不是普通文件。')
            return archive.extractfile(item), item.mode

        for name, item in members.items():
            relative = PurePosixPath(name).parts[1:]
            if not relative:
                continue
            target = destination.joinpath(*relative)
            if item.isdir():
                target.mkdir(parents=True, exist_ok=True)
                continue
            stream, mode = read_member(name)
            target.parent.mkdir(parents=True, exist_ok=True)
            with stream, target.open('xb') as output:
                shutil.copyfileobj(stream, output)
            if mode & 0o111:
                target.chmod(0o755)
                executables.add('runtime/' + '/'.join(relative))
    raw = (destination / 'bin/python3').read_bytes()[:8]
    if len(raw) != 8 or struct.unpack('<II', raw) != (0xFEEDFACF, 0x0100000C):
        raise ValueError('macOS运行时必须为Apple Silicon arm64 Python。')
    return executables


def stage_resources(destination):
    """Ship one current runtime snapshot, never the web/raw catalog or old history."""
    source = ROOT / "assets/runtime-catalog"
    if not source.is_dir():
        source = ROOT / "data/catalog"
    pointer = json.loads((source / "current.json").read_text("utf-8"))
    name = pointer["directory"]
    if not isinstance(name, str):
        raise TypeError('初装素材快照指针必须是文本。')
    name = name.replace('\\', '/')
    if (':' in name
            or len(PurePosixPath(name).parts) != 2 or PurePosixPath(name).parts[0] != 'snapshots'
            or any(part in ('', '.', '..') for part in name.split('/'))):
        raise ValueError("初装素材快照必须属于 snapshots 中的单一固定版本。")
    relative = Path(name)
    resolved = (source / relative).resolve()
    if relative.is_absolute() or not resolved.is_relative_to(source.resolve()) or not resolved.is_dir():
        raise ValueError("初装素材快照指针无效。")
    for path in resolved.rglob('*'):
        if path.is_symlink() or not path.resolve().is_relative_to(resolved):
            raise ValueError('初装素材快照包含链接或越界路径。')
    target = destination / "user-data/data/catalog"
    target.mkdir(parents=True)
    copy_tree(resolved, target / relative)
    pointer['directory'] = relative.as_posix()
    (target / 'current.json').write_text(json.dumps(pointer, ensure_ascii=False, indent=2), 'utf-8')


def trim_runtime(runtime):
    """Discard build headers/static archives and dependency test suites in this stage."""
    for path in sorted(runtime.rglob('*'), reverse=True):
        if not path.exists():
            continue
        if path.is_dir() and path.name in ('tests', '__pycache__'):
            shutil.rmtree(path)
        elif path.is_file() and path.suffix.lower() in ('.pyc', '.pyo', '.a'):
            path.unlink()
    for name in ('include', 'share/man'):
        target = runtime / name
        if target.is_dir():
            shutil.rmtree(target)


def pack_tree(folder, target, executables=()):
    files = {}
    with zipfile.ZipFile(target, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=6) as archive:
        for path in sorted(folder.rglob("*")):
            if "__pycache__" in path.parts or path.suffix.lower() in (".pyc", ".pyo"):
                continue
            if path.is_file():
                name = path.relative_to(folder).as_posix()
                info = zipfile.ZipInfo.from_file(path, name)
                info.compress_type = zipfile.ZIP_DEFLATED
                if name in executables or name.endswith('.command'):
                    info.create_system = 3
                    info.external_attr = (0o100755 << 16)
                with path.open('rb') as source, archive.open(info, 'w') as output:
                    shutil.copyfileobj(source, output)
                files[name] = file_hash(path)
    return files


def remove_build_workspace(output, folder):
    """Only discard explicitly generated children after the archives validate."""
    from ournotes.runtime.storage import inside
    output, folder = Path(output).resolve(), Path(folder).absolute()
    inside(output, folder, output)
    for path in folder.rglob('*'):
        inside(output, path, folder)
    shutil.rmtree(folder)


def build(output, version, runtime_zip, signing_key, *, install_dependencies=True, platform=None,
          initial_resources=True, vc_runtime=None, keep_staging=False, runtime_sha256=None):
    version_tuple(version)
    platform = platform or current_platform()
    if platform not in SUPPORTED_PLATFORMS:
        raise ValueError('只构建Windows x64或Apple Silicon macOS客户端。')
    if runtime_sha256 and file_hash(Path(runtime_zip)) != runtime_sha256:
        raise ValueError('Python运行时SHA-256与固定摘要不一致。')
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
    key = serialization.load_pem_private_key(Path(signing_key).read_bytes(), password=None)
    if not isinstance(key, Ed25519PrivateKey):
        raise TypeError("发布签名需要Ed25519私钥。")
    output = Path(output).resolve()
    if output == ROOT or ROOT.is_relative_to(output):
        raise ValueError("构建目录必须是独立输出目录。")
    output.mkdir(parents=True, exist_ok=True)
    stage = output / ("stage-" + platform + "-" + version)
    if stage.exists():
        raise ValueError("构建目录已存在，选择新的输出目录避免覆盖。")
    owned_workspaces = [stage]
    try:
        app, runtime = stage / "app", stage / "runtime"
        stage_source(app)
        runtime.mkdir()
        executables = extract_runtime(runtime_zip, runtime, platform)
        site = runtime / ("Lib/site-packages" if platform == 'windows-x64' else 'lib/python3.12/site-packages')
        if install_dependencies:
            command = ["uv", "export", "--locked", "--no-dev", "--no-emit-project", "--group", "client"]
            requirements = output / (platform + "-requirements.txt")
            with requirements.open("wb") as stream:
                subprocess.run(command, cwd=ROOT, stdout=stream, check=True)
            target_triple = 'x86_64-pc-windows-msvc' if platform == 'windows-x64' else 'aarch64-apple-darwin'
            subprocess.run(["uv", "pip", "install", "--python", sys.executable, "--python-version", '3.12',
                            "--python-platform", target_triple, "--target", str(site), "--requirement",
                            str(requirements), "--only-binary", ":all:", "--require-hashes"], cwd=ROOT, check=True)
            if platform == 'windows-x64':
                include_vc_runtime(runtime, vc_runtime)
                verify_vc_imports(runtime)
        site.mkdir(parents=True, exist_ok=True)
        (site / "sitecustomize.py").write_text("import os\nfrom pathlib import Path\n_dlls = []\nif os.name == 'nt':\n    for p in [Path(__file__).parent / 'pywin32_system32']:\n        if p.is_dir(): _dlls.append(os.add_dll_directory(str(p)))\n", "utf-8")
        trim_runtime(runtime)
        public = base64.b64encode(key.public_key().public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw)).decode()
        initial = output / ("client-" + platform + "-" + version)
        initial.mkdir()
        owned_workspaces.append(initial)
        shutil.copytree(stage, initial / "versions" / version)
        for name in BOOT_FILES[platform]:
            shutil.copy2(ROOT / 'deploy/client' / name, initial / name)
        shutil.copy2(ROOT / 'deploy/client/README.txt', initial / 'README.txt')
        (initial / "current.json").write_text(json.dumps({"version": version, "platform": platform}), "utf-8")
        (initial / "update-public-key.txt").write_text(public, "utf-8")
        if not initial_resources:
            raise ValueError('统一免安装包必须包含运行素材。')
        stage_resources(initial)
        artifact = output / f"ournotes-{version}-{platform}.zip"
        executable_files = {f'versions/{version}/' + name for name in executables}
        files = {path.relative_to(initial).as_posix(): file_hash(path)
                 for path in sorted(initial.rglob('*')) if path.is_file()}
        manifest = {"schema": 3, "product": "ournotes-assistant", "version": version, "channel": "stable",
                    "platform": platform, "python": PYTHON_VERSION,
                    "artifact": {"name": artifact.name}, "files": files,
                    "executables": sorted(executable_files.intersection(files))}
        envelope = {"manifest": manifest, "signature": base64.b64encode(key.sign(canonical(manifest))).decode()}
        verify(envelope, public, platform)
        (initial / 'release.json').write_text(json.dumps(envelope, ensure_ascii=False), 'utf-8')
        pack_tree(initial, artifact, executable_files)
        from ournotes.updates.portable import validate_package
        validate_package(artifact, public, platform)
        return {"artifact": str(artifact), "platform": platform}
    finally:
        if not keep_staging:
            for folder in owned_workspaces:
                if folder.exists():
                    remove_build_workspace(output, folder)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--version", required=True)
    parser.add_argument("--output", type=Path, default=ROOT / "results/build-release")
    parser.add_argument("--runtime", type=Path)
    parser.add_argument("--download-runtime", action="store_true")
    parser.add_argument("--signing-key", type=Path, required=True, help="Ed25519 PEM私钥；不进入任何构建产物")
    parser.add_argument("--generate-signing-key", action="store_true", help="仅当所选路径不存在时生成新的本地签名私钥")
    parser.add_argument("--platform", choices=SUPPORTED_PLATFORMS, default=current_platform())
    parser.add_argument("--runtime-sha256", help="已核实的Python运行时SHA-256摘要")
    parser.add_argument("--without-resources", action="store_true")
    parser.add_argument('--keep-staging', action='store_true', help='保留构建中间目录用于调试；默认验包后清理')
    parser.add_argument("--vc-runtime", type=Path, help="官方Visual Studio x64 Microsoft.VC*.CRT Redist目录；默认自动定位")
    args = parser.parse_args()
    if args.generate_signing_key:
        from cryptography.hazmat.primitives import serialization
        from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
        key = Ed25519PrivateKey.generate()
        args.signing_key.parent.mkdir(parents=True, exist_ok=True)
        with args.signing_key.open("xb") as stream:
            stream.write(key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()))
    runtime = args.runtime
    if args.download_runtime:
        if args.runtime_sha256 is None:
            args.runtime_sha256 = RUNTIME_SHA256[args.platform]
        runtime_name, url = RUNTIMES[args.platform]
        runtime = args.output / "cache" / runtime_name
        runtime.parent.mkdir(parents=True, exist_ok=True)
        if not runtime.exists():
            temporary = runtime.with_suffix(runtime.suffix + '.partial')
            try:
                urllib.request.urlretrieve(url, temporary)
                os.replace(temporary, runtime)
            finally:
                temporary.unlink(missing_ok=True)
    if runtime is None:
        parser.error("指定--runtime或明确使用--download-runtime")
    print(json.dumps(build(args.output, args.version, runtime, args.signing_key, platform=args.platform,
                           initial_resources=not args.without_resources, vc_runtime=args.vc_runtime,
                           keep_staging=args.keep_staging, runtime_sha256=args.runtime_sha256), ensure_ascii=False))


if __name__ == "__main__":
    main()
