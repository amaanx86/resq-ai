"""Builds backend/build/gateway_tools.zip: the AgentCore Gateway Lambda target (resq-gateway-tools).

Contents: gateway_tools/ (handler `gateway_tools.handler.lambda_handler`), the backend `app/` package
(the tools import app.services.*), and the Linux wheels from gateway_tools/requirements.txt.

    python backend/scripts/build_gateway_lambda.py                 # x86_64, Python 3.13 (default)
    python backend/scripts/build_gateway_lambda.py --arch arm64    # for an arm64 Lambda
    python backend/scripts/build_gateway_lambda.py --skip-deps     # offline: code only (deps from a layer)

Needs network access to PyPI for the dependencies: pip downloads manylinux wheels for the Lambda
platform (`--platform manylinux2014_<arch> --only-binary=:all:`), so it works from Windows/macOS too.
Terraform reads the zip at `${path.module}/../backend/build/gateway_tools.zip`.
"""
import argparse
import shutil
import subprocess
import sys
import tempfile
import zipfile
from pathlib import Path

BACKEND = Path(__file__).resolve().parent.parent
BUILD_DIR = BACKEND / "build"
ZIP_PATH = BUILD_DIR / "gateway_tools.zip"
REQUIREMENTS = BACKEND / "gateway_tools" / "requirements.txt"
SOURCE_PACKAGES = ("app", "gateway_tools")
EXCLUDE_DIRS = {"__pycache__", ".pytest_cache", "tests"}
EXCLUDE_SUFFIXES = {".pyc", ".pyo"}
PLATFORMS = {"x86_64": "manylinux2014_x86_64", "arm64": "manylinux2014_aarch64"}


def install_dependencies(target: Path, arch: str, python_version: str) -> None:
    cmd = [
        sys.executable, "-m", "pip", "install",
        "--requirement", str(REQUIREMENTS),
        "--target", str(target),
        "--platform", PLATFORMS[arch],
        "--implementation", "cp",
        "--python-version", python_version,
        "--only-binary=:all:",
        "--upgrade",
        "--no-compile",
        "--quiet",
    ]
    print("[build] " + " ".join(cmd))
    subprocess.run(cmd, check=True)


def copy_sources(target: Path) -> None:
    for package in SOURCE_PACKAGES:
        src = BACKEND / package
        shutil.copytree(
            src, target / package,
            ignore=lambda d, names: [n for n in names
                                     if n in EXCLUDE_DIRS or Path(n).suffix in EXCLUDE_SUFFIXES or n == ".env"],
        )


def write_zip(source_dir: Path, zip_path: Path) -> int:
    zip_path.parent.mkdir(parents=True, exist_ok=True)
    if zip_path.exists():
        zip_path.unlink()
    count = 0
    with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as zf:
        for path in sorted(source_dir.rglob("*")):
            if path.is_file() and "__pycache__" not in path.parts:
                info = zipfile.ZipInfo(path.relative_to(source_dir).as_posix())
                info.date_time = (2020, 1, 1, 0, 0, 0)  # reproducible zip -> stable Terraform source hash
                info.external_attr = 0o644 << 16
                info.compress_type = zipfile.ZIP_DEFLATED
                zf.writestr(info, path.read_bytes())
                count += 1
    return count


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--arch", choices=sorted(PLATFORMS), default="x86_64")
    parser.add_argument("--python-version", default="3.13")
    parser.add_argument("--skip-deps", action="store_true", help="code only, no pip install (offline)")
    parser.add_argument("--output", type=Path, default=ZIP_PATH)
    args = parser.parse_args()

    with tempfile.TemporaryDirectory(prefix="resq-gateway-") as tmp:
        staging = Path(tmp) / "pkg"
        staging.mkdir()
        if not args.skip_deps:
            install_dependencies(staging, args.arch, args.python_version)
        copy_sources(staging)
        files = write_zip(staging, args.output)
    size_mb = args.output.stat().st_size / 1_048_576
    print(f"[build] wrote {args.output} ({files} files, {size_mb:.1f} MB, arch={args.arch}, "
          f"deps={'no' if args.skip_deps else 'yes'})")
    print("[build] handler: gateway_tools.handler.lambda_handler  runtime: python" + args.python_version)
    return 0


if __name__ == "__main__":
    sys.exit(main())
