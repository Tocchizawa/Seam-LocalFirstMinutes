#!/usr/bin/env bash
# Native アプリへ Python バックエンドとネイティブ helper を同梱する。
# 出力先: macos/.build/backend-resources/ (SEAM_BUNDLE_RESOURCES で上書き可)
#   - uv                 : aarch64-apple-darwin の uv バイナリ
#   - seam-backend.tar.gz: src/ + pyproject.toml + uv.lock を固めた tarball
#   - audio-capture      : native audio sidecar (ScreenCaptureKit system audio + Core Audio microphone)
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
RES_DIR="${SEAM_BUNDLE_RESOURCES:-${REPO_ROOT}/macos/.build/backend-resources}"
UV_TARGET="${RES_DIR}/uv"
TAR_TARGET="${RES_DIR}/seam-backend.tar.gz"
SIDECAR_TARGET="${RES_DIR}/audio-capture"

mkdir -p "${RES_DIR}"

# ─── uv バイナリ ──────────────────────────────────────────────
# ローカル開発機の uv を採用 (バージョン揃え)。CI 化したら GitHub releases から取得する想定。
LOCAL_UV="${SEAM_UV_PATH:-$(command -v uv || true)}"
if [[ -z "${LOCAL_UV}" ]]; then
  echo "[bundle] error: 'uv' がローカルに見つかりません。インストールしてください: brew install uv" >&2
  exit 1
fi

# 既存と中身が同じならスキップ (再ビルドの高速化)
if [[ ! -f "${UV_TARGET}" ]] || ! cmp -s "${LOCAL_UV}" "${UV_TARGET}"; then
  cp "${LOCAL_UV}" "${UV_TARGET}"
  chmod +x "${UV_TARGET}"
  echo "[bundle] uv copied: $("${LOCAL_UV}" --version)"
else
  echo "[bundle] uv unchanged"
fi

# ─── Python ソース tarball ────────────────────────────────────
# 配布物に必要なファイルだけを圧縮。src/__pycache__ や .venv は当然除外。
cd "${REPO_ROOT}"
tar --exclude='__pycache__' \
    --exclude='*.pyc' \
    --exclude='.pytest_cache' \
    -czf "${TAR_TARGET}" \
    src/ pyproject.toml uv.lock

SIZE_KB=$(($(stat -f%z "${TAR_TARGET}") / 1024))
echo "[bundle] backend tarball: ${TAR_TARGET} (${SIZE_KB} KB)"

# ─── Native audio sidecar ───────────────────────────────────
if [[ "${SKIP_AUDIO_CAPTURE_SIDECAR:-0}" == "1" ]]; then
  echo "[bundle] audio-capture sidecar skipped"
else
  if ! command -v xcrun >/dev/null 2>&1; then
    echo "[bundle] error: 'xcrun' がローカルに見つかりません。Xcode Command Line Tools を確認してください" >&2
    exit 1
  fi
  CLANG="$(xcrun --find clang)"
  SDKROOT="$(xcrun --show-sdk-path)"
  "${CLANG}" \
    -isysroot "${SDKROOT}" \
    -fobjc-arc \
    -fblocks \
    -mmacosx-version-min=13.0 \
    -framework Foundation \
    -framework CoreAudio \
    -framework AudioToolbox \
    -framework ScreenCaptureKit \
    -framework CoreMedia \
    -framework CoreGraphics \
    -framework AppKit \
    "${REPO_ROOT}/sidecar/audio-capture/Sources/main.m" \
    -o "${SIDECAR_TARGET}"
  chmod +x "${SIDECAR_TARGET}"
  echo "[bundle] audio-capture sidecar copied: ${SIDECAR_TARGET}"
fi

# ─── Native Apple Speech helper ──────────────────────────────
if ! command -v swift >/dev/null 2>&1; then
  echo "[bundle] error: Swift toolchain is required to build Apple Speech helper" >&2
  exit 1
fi
swift build --package-path "${REPO_ROOT}/macos" --configuration release --product AppleSpeechTranscriber
SWIFT_BIN="$(swift build --package-path "${REPO_ROOT}/macos" --configuration release --show-bin-path)"
SPEECH_TARGET="${RES_DIR}/apple-speech-transcriber"
cp "${SWIFT_BIN}/AppleSpeechTranscriber" "${SPEECH_TARGET}"
chmod +x "${SPEECH_TARGET}"
echo "[bundle] Apple Speech helper copied: ${SPEECH_TARGET}"

echo "[bundle] OK"
