import Foundation

/// Sends a 15-second heartbeat to /presence/heartbeat so the backend knows
/// which surfaces (iPhone, Mac, etc.) are currently active.
public final class PresenceManager {
    public static let shared = PresenceManager()
    private init() {}

    private var task: Task<Void, Never>?

    public func startHeartbeat(surfaceId: String, type: String, capabilities: [String]) {
        task?.cancel()
        task = Task {
            while !Task.isCancelled {
                try? await JarvisClient.shared.heartbeat(
                    surfaceId: surfaceId,
                    type: type,
                    capabilities: capabilities
                )
                try? await Task.sleep(nanoseconds: 15_000_000_000)
            }
        }
    }

    public func stopHeartbeat() {
        task?.cancel()
        task = nil
    }
}
