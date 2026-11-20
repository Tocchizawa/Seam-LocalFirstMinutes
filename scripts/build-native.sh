#!/bin/bash
set -euo pipefail
repo="$(cd "$(dirname "$0")/.." && pwd)"
output="$repo/macos/.build/distribution"
mkdir -p "$output"
staging="$(mktemp -d "$output/.stage.XXXXXX")"
trap 'rm -rf "$staging"' EXIT

app="$staging/Seam.app"
contents="$app/Contents"
resources="$contents/Resources"
mkdir -p "$contents/MacOS" "$resources/backend"
swift build --package-path "$repo/macos" --configuration release --product SeamApp
bin="$(swift build --package-path "$repo/macos" --configuration release --show-bin-path)"
cp "$bin/SeamApp" "$contents/MacOS/SeamApp"
search_paths="$(otool -l "$contents/MacOS/SeamApp" | awk '/cmd LC_RPATH/ { getline; getline; print $2 }')"
for path in "$bin" "$bin/PackageFrameworks"; do
    if grep -qFx "$path" <<< "$search_paths"; then
        install_name_tool -delete_rpath "$path" "$contents/MacOS/SeamApp"
    fi
done
sparkle="$(find "$repo/macos/.build/artifacts" -type d -name Sparkle.framework -print -quit)"
test -n "$sparkle"
mkdir -p "$contents/Frameworks"
ditto "$sparkle" "$contents/Frameworks/Sparkle.framework"
cp "$repo/macos/.build/artifacts/macos/Sparkle/LICENSE" "$resources/Sparkle-LICENSE.txt"
cp "$repo/macos/Resources/icons/icon.icns" "$resources/icon.icns"
cp "$repo/macos/Resources/icons/SeamMenuBar.svg" "$resources/SeamMenuBar.svg"

SEAM_BUNDLE_RESOURCES="$resources" bash "$repo/scripts/bundle-backend.sh"
tar -xzf "$resources/seam-backend.tar.gz" -C "$resources/backend"
rm "$resources/seam-backend.tar.gz"

version="$(tr -d '\n' < "$repo/macos/VERSION")"
short_version="${version%%-*}"
cp "$repo/macos/Resources/Info.plist" "$contents/Info.plist"
plist="$contents/Info.plist"
plutil -insert CFBundleName -string "Seam" "$plist"
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

if [[ -n "${SPARKLE_PUBLIC_KEY:-}" ]]; then
    plutil -insert SUFeedURL -string "${SEAM_UPDATE_FEED_URL:-https://github.com/Tocchizawa/Seam-LocalFirstMinutes/releases/latest/download/appcast.xml}" "$plist"
    plutil -insert SUPublicEDKey -string "$SPARKLE_PUBLIC_KEY" "$plist"
    plutil -insert SUEnableAutomaticChecks -bool YES "$plist"
    plutil -insert SUVerifyUpdateBeforeExtraction -bool YES "$plist"
    plutil -insert SURequireSignedFeed -bool YES "$plist"
fi

identity="${APPLE_SIGNING_IDENTITY:--}"
sign=(codesign --force)
if [[ "$identity" != "-" ]]; then
    sign+=(--options runtime --timestamp)
fi
entitlements="$repo/macos/Resources/entitlements.plist"
for binary in "$resources/uv" "$resources/audio-capture" "$resources/apple-speech-transcriber"; do
    "${sign[@]}" --entitlements "$entitlements" --sign "$identity" "$binary"
done
framework="$contents/Frameworks/Sparkle.framework"
sparkle_version="$framework/Versions/Current"
for item in "$sparkle_version"/XPCServices/*.xpc "$sparkle_version/Autoupdate" "$sparkle_version/Updater.app"; do
    if [[ -e "$item" ]]; then
        "${sign[@]}" --preserve-metadata=entitlements --sign "$identity" "$item"
    fi
done
"${sign[@]}" --sign "$identity" "$framework"
"${sign[@]}" --entitlements "$entitlements" --sign "$identity" "$app"
codesign --verify --deep --strict "$app"

ln -s /Applications "$staging/Applications"
dmg="$output/Seam_${short_version}_arm64.dmg"
hdiutil create -volname "Seam" -srcfolder "$staging" -format UDZO -ov "$dmg"
if [[ "$identity" != "-" ]]; then
    codesign --force --timestamp --sign "$identity" "$dmg"
fi
destination="$output/Seam.app"
if [[ -e "$destination" ]]; then
    mv "$destination" "$staging/previous.app"
fi
mv "$app" "$destination"
echo "$destination"
echo "$dmg"
