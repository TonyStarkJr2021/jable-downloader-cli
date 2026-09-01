# Third-party software

This repository does not redistribute Chromium, FFmpeg, Playwright, FastAPI,
Uvicorn, Jinja2, Beautiful Soup or N_m3u8DL-RE. The installer obtains them from the operating-system
package manager, PyPI, or the official N_m3u8DL-RE GitHub release page. Each
dependency remains subject to its own license and terms.

On systems whose Python is too old, the installer obtains uv from Astral's
official installer and uses uv-managed Python distributions from
python-build-standalone. It does not replace the system Python.

N_m3u8DL-RE: <https://github.com/nilaoda/N_m3u8DL-RE>

uv: <https://github.com/astral-sh/uv>

python-build-standalone: <https://github.com/astral-sh/python-build-standalone>
