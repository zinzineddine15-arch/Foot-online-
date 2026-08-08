#!/usr/bin/env bash
# Aurora Live launcher: bootstraps the venv if needed, then starts the server.
set -euo pipefail
cd "$(dirname "$0")"

VENV="${HOME}/.venv"
PORT="${AURORA_PORT:-8300}"

if [ ! -x "${VENV}/bin/python" ]; then
  echo "[setup] creating virtualenv at ${VENV} ..."
  python3 -m venv "${VENV}"
fi

if ! "${VENV}/bin/python" -c "import fastapi, uvicorn, imageio_ffmpeg, PIL" 2>/dev/null; then
  echo "[setup] installing dependencies (fastapi, uvicorn, imageio-ffmpeg, pillow) ..."
  "${VENV}/bin/pip" install --quiet --upgrade pip
  "${VENV}/bin/pip" install --quiet fastapi "uvicorn[standard]" imageio-ffmpeg pillow
fi

export FFMPEG_BIN="$("${VENV}/bin/python" -c 'import imageio_ffmpeg; print(imageio_ffmpeg.get_ffmpeg_exe())')"
echo "[setup] ffmpeg: ${FFMPEG_BIN}"

if [ -f runtime/state/access_code.txt ]; then
  echo "[auth] access code: $(cat runtime/state/access_code.txt)"
elif [ -n "${ACCESS_CODE:-}" ]; then
  echo "[auth] access code: ${ACCESS_CODE}"
else
  echo "[auth] access code will be generated on first start (watch the log)"
fi

echo "[run]  serving on http://0.0.0.0:${PORT}"
exec "${VENV}/bin/python" -m uvicorn app.server:app \
  --host 0.0.0.0 --port "${PORT}" --no-access-log --log-level warning
