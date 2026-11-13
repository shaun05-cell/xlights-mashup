"""
xLights Mashup Builder — installer

Installs the Python packages listed in requirements.txt using pip (the
standard Python package installer), then checks everything works.

    python install.py            install / update the requirements
    python install.py --check    only check what's installed, change nothing

Uses only the Python standard library, so it runs on a fresh Python install.
Packages are installed for the same Python that runs this script — use that
same Python to start the app (python app.py).
"""

import importlib
import os
import subprocess
import sys

MIN_PYTHON = (3, 12)
HERE = os.path.dirname(os.path.abspath(__file__))
REQUIREMENTS = os.path.join(HERE, 'requirements.txt')

# requirement name -> module name it's imported as
IMPORT_NAMES = {'flask': 'flask', 'imageio-ffmpeg': 'imageio_ffmpeg', 'numpy': 'numpy'}


def say(msg=''):
    print(msg, flush=True)


def step(n, total, msg):
    say(f"\n[{n}/{total}] {msg}")


def pip(*args, check=True):
    """Run pip with this interpreter; returns (returncode, combined output)."""
    cmd = [sys.executable, '-m', 'pip', *args]
    proc = subprocess.run(cmd, capture_output=True, text=True)
    out = (proc.stdout or '') + (proc.stderr or '')
    if check and proc.returncode != 0:
        raise RuntimeError(out.strip()[-2000:])
    return proc.returncode, out


def check_python():
    v = sys.version_info
    say(f"    Python {v.major}.{v.minor}.{v.micro} at {sys.executable}")
    if v[:2] < MIN_PYTHON:
        say(f"    ✗ Python {MIN_PYTHON[0]}.{MIN_PYTHON[1]} or newer is required.")
        say("      Download it from https://www.python.org/downloads/ and run this again with it.")
        return False
    say("    ✓ version OK")
    return True


def ensure_pip():
    code, out = pip('--version', check=False)
    if code == 0:
        say(f"    ✓ {out.split(' from ')[0].strip()}")
        return True
    say("    pip not found — setting it up with Python's built-in ensurepip…")
    proc = subprocess.run([sys.executable, '-m', 'ensurepip', '--upgrade'],
                          capture_output=True, text=True)
    if proc.returncode != 0:
        say("    ✗ Couldn't set up pip:")
        say("      " + (proc.stderr or proc.stdout).strip()[-800:])
        say("      Reinstall Python from https://www.python.org/downloads/ with 'pip' ticked.")
        return False
    say("    ✓ pip installed")
    return True


def read_requirements():
    reqs = []
    with open(REQUIREMENTS, 'r', encoding='utf-8') as f:
        for line in f:
            line = line.split('#', 1)[0].strip()
            if line:
                reqs.append(line)
    return reqs


def install_requirements():
    say(f"    from {os.path.relpath(REQUIREMENTS)}:")
    for r in read_requirements():
        say(f"      • {r}")
    say("    (downloading from PyPI — this can take a minute the first time)")
    base = ['install', '--disable-pip-version-check', '-r', REQUIREMENTS]
    code, out = pip(*base, check=False)
    if code == 0:
        say("    ✓ packages installed")
        return True

    low = out.lower()
    in_venv = sys.prefix != getattr(sys, 'base_prefix', sys.prefix)
    if 'externally-managed-environment' in low:
        say("    ✗ This Python is managed by your operating system and won't accept pip installs.")
        say("      Create a virtual environment and run the installer with it:")
        say("        python -m venv .venv")
        say("        .venv\\Scripts\\python install.py      (Windows)")
        say("        .venv/bin/python install.py           (macOS / Linux)")
        return False
    if not in_venv and ('permission' in low or 'access is denied' in low
                        or 'errno 13' in low):
        say("    No permission to install for everyone — installing just for your user…")
        code, out = pip(*base, '--user', check=False)
        if code == 0:
            say("    ✓ packages installed (for your user)")
            return True

    say("    ✗ pip couldn't install the packages. Last lines of its output:")
    for line in out.strip().splitlines()[-15:]:
        say("      " + line)
    if 'could not find a version' in low or 'no matching distribution' in low:
        say("      Your Python version may be too new or too old for one of the packages.")
    if 'connection' in low or 'timed out' in low or 'proxy' in low:
        say("      Check your internet connection (pip downloads from https://pypi.org).")
    return False


def verify():
    ok = True
    importlib.invalidate_caches()
    from importlib.metadata import version, PackageNotFoundError
    for req, mod in IMPORT_NAMES.items():
        try:
            importlib.import_module(mod)
            try:
                ver = version(req)
            except PackageNotFoundError:
                ver = '(version unknown)'
            say(f"    ✓ {req} {ver}")
        except Exception as e:
            say(f"    ✗ {req}: {e}")
            ok = False
    if ok:
        try:
            import imageio_ffmpeg
            exe = imageio_ffmpeg.get_ffmpeg_exe()
            say(f"    ✓ ffmpeg ready ({os.path.basename(exe)})")
        except Exception as e:
            say(f"    ✗ ffmpeg not available: {e}")
            ok = False
    try:
        import tkinter  # noqa: F401  (first-run folder picker)
        say("    ✓ tkinter (folder picker)")
    except Exception:
        say("    • tkinter not available — first-run setup will ask for folders in the console instead")
    return ok


def main():
    check_only = '--check' in sys.argv
    say("xLights Mashup Builder — " + ("checking requirements" if check_only else "installer"))
    total = 2 if check_only else 4

    step(1, total, "Checking Python")
    if not check_python():
        return 1
    if not check_only:
        step(2, total, "Checking pip")
        if not ensure_pip():
            return 1
        step(3, total, "Installing packages")
        if not install_requirements():
            return 1

    step(total, total, "Verifying")
    if not verify():
        say("\n✗ Some requirements are missing." +
            ("" if not check_only else " Run:  python install.py"))
        return 1

    say("\n✓ All set!")
    say("  Start the app with:   python app.py")
    say("  First launch asks for your xLights show folder and media folder.")
    return 0


if __name__ == '__main__':
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        say("\nCancelled.")
        sys.exit(130)
