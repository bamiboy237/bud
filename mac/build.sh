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
# Ad-hoc signature: enough to run locally. A stable identity, so macOS keeps the microphone
# permission across rebuilds, arrives with hold-to-talk (THE-177).
codesign --force --sign - "$app"
echo "built $app"

if [[ ${1:-} == --run ]]; then
  pkill -x bud 2>/dev/null || true
  BUD_HOME=${BUD_HOME:-${here:h}} exec "$app/Contents/MacOS/bud"
fi
