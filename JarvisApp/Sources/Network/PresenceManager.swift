import Foundation
import UIKit

/// Sends a 15-second heartbeat to /presence/heartbeat so the backend knows
/// this iPhone is currently active and should receive response fan-out.
final class PresenceManager {
    static let shared = PresenceManager()
    private init() {}

    private var task: Task<Void, Never>?

    func startHeartbeat() {
        let deviceId = UIDevice.current.identifierForVendor?.uuidString ?? UUID().uuidString
        let surfaceId = "iphone-\(deviceId)"
        task?.cancel()
        task = Task {
            while !Task.isCancelled {
                await sendHeartbeat(surfaceId: surfaceId)
                try? await Task.sleep(nanoseconds: 15_000_000_000)
            }
        }
    }

    func stopHeartbeat() {
        task?.cancel()
        task = nil
    }

    private func sendHeartbeat(surfaceId: String) async {
        guard let url = URL(string: "\(JarvisConfig.serverURL)/presence/heartbeat") else { return }
        var req = URLRequest(url: url)
        req.httpMethod = "POST"
        req.setValue("application/json", forHTTPHeaderField: "Content-Type")
        if !JarvisConfig.apiKey.isEmpty {
            req.setValue(JarvisConfig.apiKey, forHTTPHeaderField: "X-API-Key")
        }
        let body: [String: Any] = [
            "surface_id": surfaceId,
            "type": "iphone",
            "capabilities": ["audio", "display", "push"]
        ]
        req.httpBody = try? JSONSerialization.data(withJSONObject: body)
        _ = try? await URLSession.shared.data(for: req)
    }
}
