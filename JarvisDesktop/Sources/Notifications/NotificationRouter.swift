import Foundation
import UserNotifications
import JarvisShared

/// Connects to the backend WebSocket and converts incoming DisplayPayload
/// pushes into macOS native notifications.
@MainActor
final class NotificationRouter: ObservableObject {
    private var wsTask: URLSessionWebSocketTask?

    func start() {
        requestPermission()
        connect()
    }

    private func requestPermission() {
        UNUserNotificationCenter.current().requestAuthorization(options: [.alert, .sound, .badge]) { granted, error in
            if let error { print("[NotificationRouter] Permission error: \(error)") }
        }
    }

    private func connect() {
        wsTask?.cancel()
        wsTask = JarvisClient.shared.connectWebSocket { [weak self] payload in
            Task { @MainActor [weak self] in
                self?.showNotification(payload: payload)
            }
        }
    }

    private func showNotification(payload: DisplayPayload) {
        guard payload.type != .audioOnly, !payload.title.isEmpty else { return }

        let content = UNMutableNotificationContent()
        content.title = payload.title
        content.body = payload.body.isEmpty ? " " : payload.body
        content.sound = .default

        let request = UNNotificationRequest(
            identifier: UUID().uuidString,
            content: content,
            trigger: nil
        )
        UNUserNotificationCenter.current().add(request)
    }
}
