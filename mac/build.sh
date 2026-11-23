#!/bin/zsh
# Build the bud desktop companion into mac/build/bud.app. Needs only Apple's Command Line Tools.
#   ./mac/build.sh          build
#   ./mac/build.sh --run    build, then run it against the bud checkout this script lives in
set -euo pipefail
here=${0:A:h}
app=$here/build/bud.app

swift build -c release --package-path "$here"
bin=$(swift build -c release --package-path "$here" --show-bin-path)

rm -rf "$app"
mkdir -p "$app/Contents/MacOS" "$app/Contents/Resources"
cp "$bin/BudCompanion" "$app/Contents/MacOS/bud"
cp "$here/Info.plist" "$app/Contents/Info.plist"
if [[ -d "$here/Sources/BudCompanion/Resources/Fonts" ]]; then
  mkdir -p "$app/Contents/Resources/Fonts"
  cp "$here/Sources/BudCompanion/Resources/Fonts"/* "$app/Contents/Resources/Fonts/"
  echo "fonts: bundled"
fi
# Bundled pets ship by default; local pets can replace a bundled pet with the same ID.
pets=${BUD_PETS:-${here:h}/data/pets}
pet_sheet() {
  python3 - "$1" "$2" <<'PY'
import json
import sys
from pathlib import Path

manifest = Path(sys.argv[1])
name = json.loads(manifest.read_text())[sys.argv[2]]
if not isinstance(name, str) or not name or "/" in name or name.startswith("."):
    sys.exit(f"Invalid pet image filename in {manifest}")
source = manifest.parent / name
if not source.is_file() or source.resolve().parent != manifest.parent.resolve():
    sys.exit(f"Pet image must be a file inside {manifest.parent}")
print(name)
PY
}

typeset -A pet_dirs
for dir in "$here/Resources/pets"/*(/N) "$pets"/*(/N); do
  if [[ -f "$dir/manifest.json" || -f "$dir/assets/manifest.json" || -f "$dir/pet.json" ]]; then
    pet_dirs[${dir:t}]=$dir
  fi
done
for name in ${(ok)pet_dirs}; do
  dir=${pet_dirs[$name]}; out="$app/Contents/Resources/pets/$name"
  # A manifest pet (manifest.json, at the top or in assets/) needs only the manifest and the
  # sheet it names; previews and source files stay behind.
  manifest=""
  for candidate in "$dir/manifest.json" "$dir/assets/manifest.json"; do
    [[ -f $candidate ]] && { manifest=$candidate; break; }
  done
  if [[ -n $manifest ]]; then
    atlas=$(pet_sheet "$manifest" atlas)
    mkdir -p "$out"
    cp "$manifest" "$out/manifest.json"
    cp "${manifest:h}/$atlas" "$out/"
    echo "pet: $name (manifest)"
  elif [[ -f $dir/pet.json ]]; then
    sheet=$(pet_sheet "$dir/pet.json" spritesheetPath)
    mkdir -p "$out"
    cp "$dir/pet.json" "$out/"
    cp "$dir/$sheet" "$out/"
    echo "pet: $name"
  fi
done
# OpenCode Browse (MIT) drives the owner's Chrome. The app copies it to ~/.bud/bin on launch,
# where it keeps updating itself. Pinned by version and npm's SHA-512; a build without network
# still succeeds, without the browser.
browse_version=0.1.5
typeset -A browse_sums=(
  arm64 sha512-c3jdW5KyvTkfLPThVxowNaHydLd8asiUvb85sSvfSrLRaBpECFrpNO+OICUZnebsHc/hDX9rXZ2DahkYN5+dKA==
  x86_64 sha512-ZjWAlCYzHdaMntuZeOZbKW9XEZ8sWxvpmeNzc5s5aTIEG6t2VNDmlHrER7oCg2Z+8kzLTOS29qkNhWNnsU+pbQ==
)
arch=$(uname -m)
npm_arch=${arch/x86_64/x64}
tarball=$here/build/browse-$browse_version-$arch.tgz
if [[ ! -f $tarball ]]; then
  curl -fsSL -o "$tarball.part" \
    "https://registry.npmjs.org/@opencode/browse-darwin-$npm_arch/-/browse-darwin-$npm_arch-$browse_version.tgz" \
    && mv "$tarball.part" "$tarball" || rm -f "$tarball.part"
fi
if [[ -f $tarball ]]; then
  sum="sha512-$(openssl dgst -sha512 -binary "$tarball" | base64)"
  if [[ $sum != ${browse_sums[$arch]} ]]; then
    rm -f "$tarball"
    echo "browse $browse_version: checksum mismatch, refusing it" >&2
    exit 1
  fi
  unpacked=$(mktemp -d)
  tar -xzf "$tarball" -C "$unpacked" package/bin/browse package/LICENSE
  # Helpers holds nested code, which is signed before the app below.
  mkdir -p "$app/Contents/Helpers"
  cp "$unpacked/package/bin/browse" "$app/Contents/Helpers/browse"
  cp "$unpacked/package/LICENSE" "$app/Contents/Resources/browse-LICENSE"
  rm -rf "$unpacked"
  echo "browse: $browse_version ($arch)"
else
  echo "browse: not bundled (download failed)" >&2
fi
# macOS remembers permissions such as the microphone per signing identity. An ad-hoc
# signature changes with every build, so each rebuild would ask again. With a local
# code-signing identity in the keychain (any name; set BUD_SIGN_ID), the app keeps one.
# Make one in Keychain Access: Certificate Assistant > Create a Certificate, type Code Signing.
identity=${BUD_SIGN_ID:-bud companion (local)}
helper=$app/Contents/Helpers/browse
if security find-identity -p codesigning | grep -qF "\"$identity\""; then
  [[ -f $helper ]] && codesign --force --sign "$identity" "$helper"
  codesign --force --sign "$identity" "$app"
  echo "signed: $identity"
else
  [[ -f $helper ]] && codesign --force --sign - "$helper"
  codesign --force --sign - "$app"
  echo "signed ad hoc: no \"$identity\" identity, so permissions reset each build"
fi
echo "built $app"

if [[ ${1:-} == --run ]]; then
  pkill -x bud 2>/dev/null || true
  BUD_HOME=${BUD_HOME:-${here:h}} exec "$app/Contents/MacOS/bud"
fi
