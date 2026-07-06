import AppKit
import Carbon.HIToolbox

/// Global ⌥Space hotkey — summon the Jarvis chat window from anywhere.
///
/// Uses Carbon's RegisterEventHotKey, which needs no Accessibility permission
/// (unlike NSEvent global monitors). The callback is registered once from the
/// menu-bar label view, which has @Environment(\.openWindow) access.
final class HotkeyManager {
    static let shared = HotkeyManager()
    private init() {}

    var onHotkey: (() -> Void)?

    private var hotKeyRef: EventHotKeyRef?
    private var eventHandler: EventHandlerRef?
    private var registered = false

    func register() {
        guard !registered else { return }
        registered = true

        var eventType = EventTypeSpec(
            eventClass: OSType(kEventClassKeyboard),
            eventKind: UInt32(kEventHotKeyPressed)
        )

        InstallEventHandler(
            GetApplicationEventTarget(),
            { _, _, _ -> OSStatus in
                DispatchQueue.main.async {
                    HotkeyManager.shared.onHotkey?()
                }
                return noErr
            },
            1,
            &eventType,
            nil,
            &eventHandler
        )

        let hotKeyID = EventHotKeyID(signature: OSType(0x4A52_5653), id: 1)  // 'JRVS'
        RegisterEventHotKey(
            UInt32(kVK_Space),
            UInt32(optionKey),
            hotKeyID,
            GetApplicationEventTarget(),
            0,
            &hotKeyRef
        )
        print("[HotkeyManager] ⌥Space registered")
    }

    func unregister() {
        if let ref = hotKeyRef { UnregisterEventHotKey(ref) }
        if let handler = eventHandler { RemoveEventHandler(handler) }
        hotKeyRef = nil
        eventHandler = nil
        registered = false
    }
}
