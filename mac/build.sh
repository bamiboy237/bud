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
# Pet art is private and never committed. Local pets (Codex format, one folder each with
# pet.json and spritesheet.png) go into this build only; without them the app shows a paw.
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

for dir in "$pets"/*(/N); do
  name=${dir:t}; out="$app/Contents/Resources/pets/$name"
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
# macOS remembers permissions such as the microphone per signing identity. An ad-hoc
# signature changes with every build, so each rebuild would ask again. With a local
# code-signing identity in the keychain (any name; set BUD_SIGN_ID), the app keeps one.
# Make one in Keychain Access: Certificate Assistant > Create a Certificate, type Code Signing.
identity=${BUD_SIGN_ID:-bud companion (local)}
if security find-identity -p codesigning | grep -qF "\"$identity\""; then
  codesign --force --sign "$identity" "$app"
  echo "signed: $identity"
else
  codesign --force --sign - "$app"
  echo "signed ad hoc: no \"$identity\" identity, so permissions reset each build"
fi
echo "built $app"

if [[ ${1:-} == --run ]]; then
  pkill -x bud 2>/dev/null || true
  BUD_HOME=${BUD_HOME:-${here:h}} exec "$app/Contents/MacOS/bud"
fi
