#!/bin/bash
set -euo pipefail
repo="$(cd "$(dirname "$0")/.." && pwd)"
output="$repo/macos/.build/distribution"
mkdir -p "$output"
staging="$(mktemp -d "$output/.stage.XXXXXX")"
trap 'rm -rf "$staging"' EXIT

app="$staging/Seam Native.app"
contents="$app/Contents"
resources="$contents/Resources"
mkdir -p "$contents/MacOS" "$resources/backend"
swift build --package-path "$repo/macos" --configuration release --product SeamApp
bin="$(swift build --package-path "$repo/macos" --configuration release --show-bin-path)"
cp "$bin/SeamApp" "$contents/MacOS/SeamApp"
cp "$repo/gui/src-tauri/icons/icon.icns" "$resources/icon.icns"

SEAM_BUNDLE_RESOURCES="$resources" bash "$repo/scripts/bundle-backend.sh"
tar -xzf "$resources/seam-backend.tar.gz" -C "$resources/backend"
rm "$resources/seam-backend.tar.gz"

version="$(/usr/bin/plutil -extract version raw "$repo/gui/src-tauri/tauri.conf.json")"
short_version="${version%%-*}"
cp "$repo/gui/src-tauri/Info.plist" "$contents/Info.plist"
plist="$contents/Info.plist"
plutil -insert CFBundleName -string "Seam Native" "$plist"
plutil -insert CFBundleDisplayName -string "Seam" "$plist"
plutil -insert CFBundleIdentifier -string com.seamapp.seam.native "$plist"
plutil -insert CFBundlePackageType -string APPL "$plist"
plutil -insert CFBundleExecutable -string SeamApp "$plist"
plutil -insert CFBundleIconFile -string icon.icns "$plist"
plutil -insert CFBundleShortVersionString -string "$short_version" "$plist"
plutil -insert CFBundleVersion -string "$short_version" "$plist"
plutil -insert LSMinimumSystemVersion -string 26.0 "$plist"
plutil -insert NSHighResolutionCapable -bool YES "$plist"
plutil -insert NSAppTransportSecurity -json '{"NSAllowsLocalNetworking":true}' "$plist"

identity="${APPLE_SIGNING_IDENTITY:--}"
sign=(codesign --force)
if [[ "$identity" != "-" ]]; then
    sign+=(--options runtime --timestamp)
fi
entitlements="$repo/gui/src-tauri/entitlements.plist"
for binary in "$resources/uv" "$resources/audio-capture"; do
    "${sign[@]}" --entitlements "$entitlements" --sign "$identity" "$binary"
done
"${sign[@]}" --entitlements "$entitlements" --sign "$identity" "$app"
codesign --verify --deep --strict "$app"

ln -s /Applications "$staging/Applications"
dmg="$output/Seam_Native_${short_version}_arm64.dmg"
hdiutil create -volname "Seam Native" -srcfolder "$staging" -format UDZO -ov "$dmg"
destination="$output/Seam Native.app"
if [[ -e "$destination" ]]; then
    mv "$destination" "$staging/previous.app"
fi
mv "$app" "$destination"
echo "$destination"
echo "$dmg"
