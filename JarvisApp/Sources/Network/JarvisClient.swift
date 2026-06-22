import Foundation

// MARK: - Configuration

struct JarvisConfig {
    static var serverURL: String {
        get { UserDefaults.group.string(forKey: "serverURL") ?? "http://192.168.1.100:8080" }
        set { UserDefaults.group.set(newValue, forKey: "serverURL") }
    }
    static var apiKey: String {
        get { UserDefaults.group.string(forKey: "apiKey") ?? "" }
        set { UserDefaults.group.set(newValue, forKey: "apiKey") }
    }
}

extension UserDefaults {
    static let group = UserDefaults(suiteName: "group.com.jarvis.app") ?? .standard
}

// MARK: - Client

final class JarvisClient {
    static let shared = JarvisClient()
    private init() {}

    private let session = URLSession.shared

    // MARK: Text query → QueryResponse

    func askText(_ text: String) async throws -> QueryResponse {
        let body = TextQueryRequest(text: text)
        return try await post(path: "/ask/query", body: body)
    }

    // MARK: Audio query → QueryResponse

    func askAudio(_ audioData: Data) async throws -> QueryResponse {
        var request = baseRequest(path: "/ask/query/audio")
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

    // MARK: Siri intent — plain text response only (no audio synthesis)

    func askTextForSiri(_ text: String) async throws -> String {
        let body = TextQueryRequest(text: text)
        let response: QueryResponse = try await post(path: "/ask/query", body: body)
        return response.text
    }

    // MARK: WebSocket — receives DisplayPayload pushes from backend

    func connectWebSocket(onMessage: @escaping (DisplayPayload) -> Void) -> URLSessionWebSocketTask {
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

    private func baseRequest(path: String) -> URLRequest {
        var req = URLRequest(url: URL(string: "\(JarvisConfig.serverURL)\(path)")!)
        if !JarvisConfig.apiKey.isEmpty {
            req.setValue(JarvisConfig.apiKey, forHTTPHeaderField: "X-API-Key")
        }
        return req
    }

    private func post<Body: Encodable, Response: Decodable>(path: String, body: Body) async throws -> Response {
        var request = baseRequest(path: path)
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

// MARK: - Errors

enum JarvisError: LocalizedError {
    case invalidResponse
    case httpError(Int, String)

    var errorDescription: String? {
        switch self {
        case .invalidResponse: return "Invalid response from Jarvis server."
        case .httpError(let code, let detail): return "Server error \(code): \(detail)"
        }
    }
}
