# Sprite checks

On macOS, run from the repository root:

```sh
swift test -c release --package-path mac
```

Swift Testing exercises the production sprite loader and frame selector with
small synthetic PNG fixtures. It checks optional-entry fallback, one-shot timing,
consecutive reactions, pose priority, and the panel's fixed display box.

The tests do not load private pet art, start the companion, connect to Bud, or
use the microphone.
