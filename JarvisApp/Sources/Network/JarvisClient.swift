import Foundation

// MARK: - Configuration

struct JarvisConfig {
    static var serverURL: String {
        get {
            let raw = UserDefaults.group.string(forKey: "serverURL") ?? "http://192.168.1.100:8080"
            return Self.normalized(raw)
        }
        set { UserDefaults.group.set(newValue, forKey: "serverURL") }
    }

    /// A bare hostname pasted into Settings ("host.ts.net:8080") produces
    /// NSURLError -1002 "unsupported URL" on every request — including the
    /// Siri intent, which then dismisses silently. Always ensure a scheme.
    static func normalized(_ raw: String) -> String {
        var url = raw.components(separatedBy: .whitespacesAndNewlines).joined()
        while url.hasSuffix("/") { url.removeLast() }
        if !url.lowercased().hasPrefix("http://") && !url.lowercased().hasPrefix("https://") {
            url = "http://" + url
        }
        return url
    }
    static var apiKey: String {
        get { UserDefaults.group.string(forKey: "apiKey") ?? "" }
        set { UserDefaults.group.set(newValue, forKey: "apiKey") }
    }
}

extension UserDefaults {
    static let group = UserDefaults(suiteName: "group.com.omarsalazar.jarvis") ?? .standard
}

// MARK: - Client

final class JarvisClient {
    static let shared = JarvisClient()
    private init() {}

    private let session = URLSession.shared

    // MARK: Text query → QueryResponse

    func askText(_ text: String, speak: Bool = true) async throws -> QueryResponse {
        let body = TextQueryRequest(text: text, speak: speak)
        return try await post(path: "/ask/query", body: body)
    }

    // MARK: Audio query → QueryResponse

    func askAudio(_ audioData: Data) async throws -> QueryResponse {
        var request = try baseRequest(path: "/ask/query/audio")
        request.httpMethod = "POST"
        request.setValue("application/octet-stream", forHTTPHeaderField: "Content-Type")
        request.httpBody = audioData
        return try await execute(request)
    }

    // MARK: Image + optional text → QueryResponse

    func askImage(_ imageData: Data, text: String = "What am I looking at?") async throws -> QueryResponse {
        let body = ImageQueryRequest(imageB64: imageData.base64EncodedString(), text: text)
        return try await post(path: "/ask/image", body: body)
    }

    // MARK: Video query → QueryResponse
    //
    // The backend prefers Gemini for true video understanding (motion, temporal
    // order, audio) and falls back to sampling 6 frames with ffmpeg for Claude,
    // so this works whether or not a Gemini key is funded. Keep clips short
    // (~5-20s): the whole file is base64'd into the request body.

    func askVideo(_ videoData: Data, text: String = "What is happening in this video?") async throws -> QueryResponse {
        let body = VideoQueryRequest(videoB64: videoData.base64EncodedString(), text: text)
        return try await post(path: "/ask/video", body: body)
    }

    // MARK: Tool events — for the activity timeline

    func fetchToolEvents(limit: Int = 25) async throws -> [ToolEvent] {
        let request = try baseRequest(path: "/tool-events?limit=\(limit)")
        let response: ToolEventsResponse = try await execute(request)
        return response.events
    }

    /// Every tool call belonging to one turn.
    ///
    /// Used to reconcile a message after its reply lands: the live stream can
    /// drop, or deliver events before the client knows the turn id, so the
    /// inline list is confirmed against the server rather than trusted.
    func fetchToolEvents(turnID: String) async throws -> [ToolEvent] {
        let escaped = turnID.addingPercentEncoding(
            withAllowedCharacters: .urlQueryAllowed) ?? turnID
        let request = try baseRequest(path: "/tool-events?turn_id=\(escaped)")
        let response: ToolEventsResponse = try await execute(request)
        return response.events
    }

    // MARK: Live tool-call stream (SSE)

    /// Tool calls as they happen, so a long multi-tool turn shows progress
    /// instead of a spinner.
    ///
    /// This is a live tail with no backlog — `/tool-events` still holds the
    /// durable history, so a reconnect backfills from there rather than
    /// replaying the stream.
    func toolEventStream() -> AsyncThrowingStream<ToolEvent, Error> {
        AsyncThrowingStream { continuation in
            let task = Task {
                do {
                    var request = try baseRequest(path: "/stream/tools")
                    // A long-lived stream must not trip the default timeout;
                    // the server sends comment frames to keep it warm.
                    request.timeoutInterval = .infinity
                    let (bytes, response) = try await URLSession.shared.bytes(for: request)
                    guard let http = response as? HTTPURLResponse else {
                        throw JarvisError.invalidResponse
                    }
                    guard http.statusCode == 200 else {
                        throw JarvisError.httpError(http.statusCode, "tool stream")
                    }
                    let decoder = JSONDecoder()
                    for try await line in bytes.lines {
                        // SSE frames: "data: {json}". Lines starting with ":"
                        // are keepalive comments and are meant to be ignored.
                        guard line.hasPrefix("data:") else { continue }
                        let payload = line.dropFirst(5)
                            .trimmingCharacters(in: .whitespaces)
                        guard let data = payload.data(using: .utf8),
                              let event = try? decoder.decode(ToolEvent.self, from: data)
                        else { continue }
                        continuation.yield(event)
                    }
                    continuation.finish()
                } catch {
                    continuation.finish(throwing: error)
                }
            }
            continuation.onTermination = { _ in task.cancel() }
        }
    }

    // MARK: Siri intent — plain text response only (no audio synthesis)

    private struct SiriResponse: Codable { let text: String }

    func askTextForSiri(_ text: String) async throws -> String {
        let body = TextQueryRequest(text: text)
        // /ask/query/siri skips server-side WAV synthesis — Siri speaks the
        // dialog itself, and the saved seconds prevent Siri timeouts.
        let response: SiriResponse = try await post(path: "/ask/query/siri", body: body)
        return response.text
    }

    // MARK: Agents feed — reports without spending LLM tokens

    struct AgentFeedItem: Codable, Identifiable {
        let name: String
        let displayName: String
        let persona: String
        let description: String
        let enabled: Bool
        let status: String
        let lastRun: String
        let report: String
        var id: String { name }

        enum CodingKeys: String, CodingKey {
            case name, persona, description, enabled, status, report
            case displayName = "display_name"
            case lastRun = "last_run"
        }
    }

    private struct AgentFeedResponse: Codable { let agents: [AgentFeedItem] }

    func fetchAgentFeed() async throws -> [AgentFeedItem] {
        let request = try baseRequest(path: "/agents/feed")
        let response: AgentFeedResponse = try await execute(request)
        return response.agents
    }

    // MARK: Dev mode — unified agent activity stream (tool/thinking/finding)

    struct AgentEvent: Codable, Identifiable {
        let agent: String
        let kind: String        // tool | thinking | finding
        let text: String
        let ts: String
        var id: String { "\(agent)-\(ts)-\(text.hashValue)" }
    }

    private struct AgentEventsResponse: Codable { let events: [AgentEvent] }

    func fetchAgentEvents(limit: Int = 150) async throws -> [AgentEvent] {
        let request = try baseRequest(path: "/agents/events?limit=\(limit)")
        let response: AgentEventsResponse = try await execute(request)
        return response.events
    }

    // MARK: Conversation history — restore chat across app launches

    struct HistoryMessage: Codable {
        let role: String
        let text: String
    }

    private struct HistoryResponse: Codable { let messages: [HistoryMessage] }

    func fetchHistory(limit: Int = 40) async throws -> [HistoryMessage] {
        let request = try baseRequest(path: "/history?limit=\(limit)")
        let response: HistoryResponse = try await execute(request)
        return response.messages
    }

    private struct FaceEnrollResponse: Decodable {
        let name: String
        let samples: Int
        let of: Int
        let enrolled: Bool
    }

    /// Teach Jarvis a face from selfies — the vision service extracts an
    /// embedding per usable image and averages them into the identity Sentry's
    /// camera recognition compares against. Re-enrolling a name replaces it.
    func enrollFace(name: String, imagesB64: [String]) async throws
        -> (samples: Int, of: Int, enrolled: Bool) {
        var request = try baseRequest(path: "/face/enroll")
        request.httpMethod = "POST"
        request.setValue("application/json", forHTTPHeaderField: "Content-Type")
        request.httpBody = try JSONSerialization.data(withJSONObject: [
            "name": name, "images": imagesB64, "finalize": true,
        ])
        request.timeoutInterval = 90   // embedding on CPU takes seconds/image
        let response: FaceEnrollResponse = try await execute(request)
        return (response.samples, response.of, response.enrolled)
    }

    private struct PushesResponse: Decodable { let pushes: [PushItem] }

    /// Proactive pushes persisted server-side (Sentry cards etc.) — the
    /// WebSocket only reaches a foregrounded app, so anything sent while
    /// suspended is recovered from this feed on launch/foreground.
    func fetchPushes(limit: Int = 20) async throws -> [PushItem] {
        let request = try baseRequest(path: "/pushes?limit=\(limit)")
        let response: PushesResponse = try await execute(request)
        return response.pushes
    }

    // MARK: HealthKit snapshot — push fitness metrics to the backend

    @discardableResult
    func pushHealthSnapshot(_ snapshot: HealthSnapshot) async throws -> Bool {
        var request = try baseRequest(path: "/health/snapshot")
        request.httpMethod = "POST"
        request.setValue("application/json", forHTTPHeaderField: "Content-Type")
        request.httpBody = try JSONEncoder().encode(snapshot)
        let (_, response) = try await session.data(for: request)
        guard let http = response as? HTTPURLResponse, (200..<300).contains(http.statusCode) else {
            return false
        }
        return true
    }

    // MARK: Calendar — push next upcoming event so the ambient agent can warn

    @discardableResult
    func pushNextCalendarEvent(_ event: NextCalendarEvent) async throws -> Bool {
        var request = try baseRequest(path: "/calendar/next-event")
        request.httpMethod = "POST"
        request.setValue("application/json", forHTTPHeaderField: "Content-Type")
        request.httpBody = try JSONEncoder().encode(event)
        let (_, response) = try await session.data(for: request)
        guard let http = response as? HTTPURLResponse, (200..<300).contains(http.statusCode) else {
            return false
        }
        return true
    }

    // MARK: Helpers

    private func baseRequest(path: String) throws -> URLRequest {
        let full = "\(JarvisConfig.serverURL)\(path)"
        guard let url = URL(string: full) else {
            throw JarvisError.invalidURL(JarvisConfig.serverURL)
        }
        var req = URLRequest(url: url)
        if !JarvisConfig.apiKey.isEmpty {
            req.setValue(JarvisConfig.apiKey, forHTTPHeaderField: "X-API-Key")
        }
        return req
    }

    private func post<Body: Encodable, Response: Decodable>(path: String, body: Body) async throws -> Response {
        var request = try baseRequest(path: path)
        request.httpMethod = "POST"
        request.setValue("application/json", forHTTPHeaderField: "Content-Type")
        request.httpBody = try JSONEncoder().encode(body)
        return try await execute(request)
    }

    private func execute<Response: Decodable>(_ request: URLRequest) async throws -> Response {
        let (data, response) = try await session.data(for: request)
        guard let http = response as? HTTPURLResponse else {
            throw JarvisError.invalidResponse
        }
        guard (200..<300).contains(http.statusCode) else {
            let detail = String(data: data, encoding: .utf8) ?? "No detail"
            throw JarvisError.httpError(http.statusCode, detail)
        }
        return try JSONDecoder().decode(Response.self, from: data)
    }
}

// MARK: - Reconnecting WebSocket

/// Owns the push WebSocket and reconnects with exponential backoff (2s → 60s).
/// The raw task dies silently on any error — proactive pushes then stop until
/// app relaunch. A 30s ping detects dead connections (e.g. after backgrounding).
final class JarvisSocket {
    private var task: URLSessionWebSocketTask?
    private var onMessage: ((DisplayPayload) -> Void)?
    private var backoff: TimeInterval = 2
    private var stopped = false
    private var pingTimer: Timer?

    func start(onMessage: @escaping (DisplayPayload) -> Void) {
        self.onMessage = onMessage
        stopped = false
        connect()
    }

    func stop() {
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
                self.backoff = 2
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

// MARK: - Errors

enum JarvisError: LocalizedError {
    case invalidResponse
    case invalidURL(String)
    case httpError(Int, String)

    var errorDescription: String? {
        switch self {
        case .invalidResponse: return "Invalid response from Jarvis server."
        case .invalidURL(let u):
            return "Server URL isn't valid: \"\(u)\". Expected something like http://host:8080"
        case .httpError(let code, let detail): return "Server error \(code): \(detail)"
        }
    }
}
