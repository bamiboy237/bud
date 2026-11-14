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
for pet in "$pets"/*/pet.json(N); do
  dir=${pet:h}; name=${dir:t}
  mkdir -p "$app/Contents/Resources/pets/$name"
  cp "$dir/pet.json" "$app/Contents/Resources/pets/$name/"
  sheet=$(python3 -c "import json,sys;print(json.load(open(sys.argv[1]))['spritesheetPath'])" "$pet")
  cp "$dir/$sheet" "$app/Contents/Resources/pets/$name/"
  echo "pet: $name"
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
