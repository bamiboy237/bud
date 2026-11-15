import AppKit
import Carbon.HIToolbox

/// A system-wide shortcut that reports both the press and the release, which hold-to-talk
/// needs. Carbon's hot-key API is old, but it's still the one macOS offers for this: it works
/// while another app is in front and needs no extra permission, unlike watching every key.
@MainActor
final class Hotkey {
    private let id: UInt32
    private var ref: EventHotKeyRef?
    private let onPress: () -> Void
    private let onRelease: () -> Void

    private static var registered: [UInt32: Hotkey] = [:]
    private static var nextID: UInt32 = 1
    private static var handler: EventHandlerRef?

    /// Nil when the shortcut can't be had, usually because another app already owns it.
    init?(key: Int, modifiers: Int, onPress: @escaping () -> Void, onRelease: @escaping () -> Void = {}) {
        Self.installHandler()
        id = Self.nextID
        Self.nextID += 1
        self.onPress = onPress
        self.onRelease = onRelease
        // "bud " as a four-character code, so the events are recognisably ours.
        let hotKeyID = EventHotKeyID(signature: OSType(0x6275_6420), id: id)
        var ref: EventHotKeyRef?
        // Exclusive, so a shortcut another app already owns fails here instead of registering
        // quietly and never firing.
        let status = RegisterEventHotKey(
            UInt32(key), UInt32(modifiers), hotKeyID, GetApplicationEventTarget(),
            OptionBits(kEventHotKeyExclusive), &ref)
        guard status == noErr, let ref else { return nil }
        self.ref = ref
        Self.registered[id] = self
    }

    func unregister() {
        if let ref { UnregisterEventHotKey(ref) }
        ref = nil
        Self.registered[id] = nil
    }

    private static func installHandler() {
        guard handler == nil else { return }
        let specs = [
            EventTypeSpec(eventClass: OSType(kEventClassKeyboard), eventKind: UInt32(kEventHotKeyPressed)),
            EventTypeSpec(eventClass: OSType(kEventClassKeyboard), eventKind: UInt32(kEventHotKeyReleased)),
        ]
        // A C callback can't capture anything, so it looks the shortcut up by its id.
        InstallEventHandler(
            GetApplicationEventTarget(),
            { _, event, _ in
                var hotKeyID = EventHotKeyID()
                GetEventParameter(
                    event, EventParamName(kEventParamDirectObject), EventParamType(typeEventHotKeyID),
                    nil, MemoryLayout<EventHotKeyID>.size, nil, &hotKeyID)
                let pressed = GetEventKind(event) == UInt32(kEventHotKeyPressed)
                let id = hotKeyID.id
                // Carbon delivers these on the main run loop.
                MainActor.assumeIsolated {
                    guard let hotkey = Hotkey.registered[id] else { return }
                    pressed ? hotkey.onPress() : hotkey.onRelease()
                }
                return noErr
            },
            specs.count, specs, nil, &handler)
    }
}

/// Holding the right Option key on its own, for when another app owns ⌥ Space. macOS only
/// shows other apps' modifier keys to an app with the Accessibility permission, so `allowed`
/// says whether it can work, and `askForPermission` opens the prompt.
@MainActor
final class RightOptionHold {
    static var allowed: Bool { AXIsProcessTrusted() }

    static func askForPermission() {
        let prompt = "AXTrustedCheckOptionPrompt" as CFString
        _ = AXIsProcessTrustedWithOptions([prompt: true] as CFDictionary)
    }

    private var monitors: [Any] = []
    private var down = false

    init(onPress: @escaping () -> Void, onRelease: @escaping () -> Void) {
        let handle: (NSEvent) -> Void = { [weak self] event in
            guard let self else { return }
            // The right key's own bit (NX_DEVICERALTKEYMASK), not the shared Option flag, which
            // stays set while the left Option key is still down.
            let held = event.modifierFlags.rawValue & 0x40 != 0
            if held, !down {
                down = true
                onPress()
            } else if !held, down {
                down = false
                onRelease()
            }
        }
        if let global = NSEvent.addGlobalMonitorForEvents(matching: .flagsChanged, handler: handle) {
            monitors.append(global)
        }
        if let local = NSEvent.addLocalMonitorForEvents(matching: .flagsChanged, handler: { handle($0); return $0 }) {
            monitors.append(local)
        }
    }

    func stop() {
        monitors.forEach(NSEvent.removeMonitor)
        monitors = []
    }
}
