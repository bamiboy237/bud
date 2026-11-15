# Sprite checks

On macOS, run from the repository root:

```sh
swift test -c release --package-path mac
```

Swift Testing exercises the production sprite loader and frame selector with
small synthetic PNG fixtures. It checks optional-entry fallback, one-shot timing,
consecutive reactions, pose priority, and the panel's fixed display box.
The bundled dog checks load the public artwork, verify its hash and runtime-only
manifest, and exercise all ten app poses.

The tests do not start the companion, connect to Bud, or use the microphone.

## Check the packaged artwork

The macOS CI job also runs these commands from the repository root:

```sh
./mac/build.sh
for source in mac/Resources/pets/dog/*; do
  cmp "$source" "mac/build/bud.app/Contents/Resources/pets/dog/${source##*/}"
done
test "$(find mac/build/bud.app/Contents/Resources/pets/dog -type f | wc -l)" -eq 2
codesign --verify --deep --strict mac/build/bud.app
```

Run the comparison in a clean checkout without local pet overrides.
