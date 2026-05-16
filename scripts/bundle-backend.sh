#!/usr/bin/env bash
# Tauri ビルド前に走らせる「Python バックエンドを .app に同梱する」スクリプト。
# 出力先: gui/src-tauri/resources/
#   - uv                 : aarch64-apple-darwin の uv バイナリ
#   - seam-backend.tar.gz: src/ + pyproject.toml + uv.lock を固めた tarball
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
RES_DIR="${REPO_ROOT}/gui/src-tauri/resources"
UV_TARGET="${RES_DIR}/uv"
TAR_TARGET="${RES_DIR}/seam-backend.tar.gz"

mkdir -p "${RES_DIR}"

# ─── uv バイナリ ──────────────────────────────────────────────
# ローカル開発機の uv を採用 (バージョン揃え)。CI 化したら GitHub releases から取得する想定。
LOCAL_UV="$(command -v uv || true)"
if [[ -z "${LOCAL_UV}" ]]; then
  echo "[bundle] error: 'uv' がローカルに見つかりません。インストールしてください: brew install uv" >&2
  exit 1
fi

# 既存と中身が同じならスキップ (再ビルドの高速化)
if [[ ! -f "${UV_TARGET}" ]] || ! cmp -s "${LOCAL_UV}" "${UV_TARGET}"; then
  cp "${LOCAL_UV}" "${UV_TARGET}"
  chmod +x "${UV_TARGET}"
  echo "[bundle] uv copied: $(${LOCAL_UV} --version)"
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

echo "[bundle] OK"
