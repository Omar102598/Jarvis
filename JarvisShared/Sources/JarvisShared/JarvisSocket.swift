import Foundation

/// Reconnecting WebSocket for backend pushes.
///
/// The raw `connectWebSocket` task dies silently on any error — proactive
/// pushes then stop until app relaunch. This wrapper owns the task, pings to
/// detect dead connections, and reconnects with exponential backoff (2s → 60s).
public final class JarvisSocket {
    private var task: URLSessionWebSocketTask?
    private var onMessage: ((DisplayPayload) -> Void)?
    private var backoff: TimeInterval = 2
    private var stopped = false
    private var pingTimer: Timer?

    public init() {}

    public func start(onMessage: @escaping (DisplayPayload) -> Void) {
        self.onMessage = onMessage
        stopped = false
        connect()
    }

    public func stop() {
        stopped = true
        pingTimer?.invalidate()
        task?.cancel(with: .goingAway, reason: nil)
        task = nil
    }

    private func connect() {
        guard !stopped else { return }
        let wsURL = JarvisConfig.serverURL
            .replacingOccurrences(of: "http://", with: "ws://")
            .replacingOccurrences(of: "https://", with: "wss://")
        guard let url = URL(string: "\(wsURL)/ws/glasses") else { return }
        var request = URLRequest(url: url)
        if !JarvisConfig.apiKey.isEmpty {
            request.setValue(JarvisConfig.apiKey, forHTTPHeaderField: "X-API-Key")
        }
        let t = URLSession.shared.webSocketTask(with: request)
        task = t
        t.resume()
        receive(on: t)
        schedulePing()
    }

    private func receive(on t: URLSessionWebSocketTask) {
        t.receive { [weak self] result in
            guard let self, !self.stopped, self.task === t else { return }
            switch result {
            case .success(let message):
                self.backoff = 2  // healthy — reset backoff
                var data: Data?
                if case .string(let text) = message { data = text.data(using: .utf8) }
                if case .data(let d) = message { data = d }
                if let data, let payload = try? JSONDecoder().decode(DisplayPayload.self, from: data) {
                    self.onMessage?(payload)
                }
                self.receive(on: t)
            case .failure(let error):
                print("[JarvisSocket] error: \(error) — reconnecting in \(self.backoff)s")
                self.scheduleReconnect()
            }
        }
    }

    private func scheduleReconnect() {
        guard !stopped else { return }
        pingTimer?.invalidate()
        task?.cancel()
        task = nil
        let delay = backoff
        backoff = min(backoff * 2, 60)
        DispatchQueue.main.asyncAfter(deadline: .now() + delay) { [weak self] in
            self?.connect()
        }
    }

    private func schedulePing() {
        pingTimer?.invalidate()
        pingTimer = Timer.scheduledTimer(withTimeInterval: 30, repeats: true) { [weak self] _ in
            guard let self, let t = self.task, !self.stopped else { return }
            t.sendPing { [weak self] error in
                if error != nil { self?.scheduleReconnect() }
            }
        }
    }
}
