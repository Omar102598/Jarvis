import Foundation

// MARK: - Errors

public enum JarvisError: LocalizedError {
    case invalidResponse
    case httpError(Int, String)

    public var errorDescription: String? {
        switch self {
        case .invalidResponse: return "Invalid response from Jarvis server."
        case .httpError(let code, let detail): return "Server error \(code): \(detail)"
        }
    }
}

// MARK: - Client

public final class JarvisClient {
    public static let shared = JarvisClient()
    private init() {}

    private let session = URLSession.shared

    // MARK: Text query → QueryResponse

    public func askText(_ text: String, source: String = "mobile") async throws -> QueryResponse {
        let body = TextQueryRequest(text: text, source: source)
        return try await post(path: "/ask/query", body: body)
    }

    // MARK: Audio query → QueryResponse

    public func askAudio(_ audioData: Data) async throws -> QueryResponse {
        var request = baseRequest(path: "/ask/query/audio")
        request.httpMethod = "POST"
        request.setValue("application/octet-stream", forHTTPHeaderField: "Content-Type")
        request.httpBody = audioData
        return try await execute(request)
    }

    // MARK: Image + optional text → QueryResponse

    public func askImage(_ imageData: Data, text: String = "What am I looking at?") async throws -> QueryResponse {
        let body = ImageQueryRequest(imageB64: imageData.base64EncodedString(), text: text)
        return try await post(path: "/ask/image", body: body)
    }

    // MARK: Siri intent — plain text response only

    public func askTextForSiri(_ text: String) async throws -> String {
        let response: QueryResponse = try await askText(text)
        return response.text
    }

    // MARK: Conversation history — restore chat across app launches

    public struct HistoryMessage: Codable {
        public let role: String
        public let text: String
    }

    private struct HistoryResponse: Codable {
        let messages: [JarvisClient.HistoryMessage]
    }

    public func fetchHistory(limit: Int = 40) async throws -> [HistoryMessage] {
        let request = baseRequest(path: "/history?limit=\(limit)")
        let response: HistoryResponse = try await execute(request)
        return response.messages
    }

    // MARK: Presence heartbeat

    public func heartbeat(surfaceId: String, type: String, capabilities: [String]) async throws {
        let body = HeartbeatRequest(surfaceId: surfaceId, type: type, capabilities: capabilities)
        var req = baseRequest(path: "/presence/heartbeat")
        req.httpMethod = "POST"
        req.setValue("application/json", forHTTPHeaderField: "Content-Type")
        req.httpBody = try JSONEncoder().encode(body)
        let (_, response) = try await session.data(for: req)
        guard let http = response as? HTTPURLResponse, (200..<300).contains(http.statusCode) else { return }
    }

    // MARK: WebSocket — receives DisplayPayload pushes from backend

    public func connectWebSocket(onMessage: @escaping (DisplayPayload) -> Void) -> URLSessionWebSocketTask {
        let wsURL = JarvisConfig.serverURL
            .replacingOccurrences(of: "http://", with: "ws://")
            .replacingOccurrences(of: "https://", with: "wss://")
        var request = URLRequest(url: URL(string: "\(wsURL)/ws/glasses")!)
        if !JarvisConfig.apiKey.isEmpty {
            request.setValue(JarvisConfig.apiKey, forHTTPHeaderField: "X-API-Key")
        }
        let task = session.webSocketTask(with: request)

        func receive() {
            task.receive { result in
                switch result {
                case .success(.string(let text)):
                    if let data = text.data(using: .utf8),
                       let payload = try? JSONDecoder().decode(DisplayPayload.self, from: data) {
                        onMessage(payload)
                    }
                    receive()
                case .success(.data(let data)):
                    if let payload = try? JSONDecoder().decode(DisplayPayload.self, from: data) {
                        onMessage(payload)
                    }
                    receive()
                case .failure(let error):
                    print("[JarvisClient] WebSocket error: \(error)")
                @unknown default:
                    receive()
                }
            }
        }

        task.resume()
        receive()
        return task
    }

    // MARK: Helpers

    public func baseRequest(path: String) -> URLRequest {
        var req = URLRequest(url: URL(string: "\(JarvisConfig.serverURL)\(path)")!)
        if !JarvisConfig.apiKey.isEmpty {
            req.setValue(JarvisConfig.apiKey, forHTTPHeaderField: "X-API-Key")
        }
        return req
    }

    public func post<Body: Encodable, Response: Decodable>(path: String, body: Body) async throws -> Response {
        var request = baseRequest(path: path)
        request.httpMethod = "POST"
        request.setValue("application/json", forHTTPHeaderField: "Content-Type")
        request.httpBody = try JSONEncoder().encode(body)
        return try await execute(request)
    }

    public func execute<Response: Decodable>(_ request: URLRequest) async throws -> Response {
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
