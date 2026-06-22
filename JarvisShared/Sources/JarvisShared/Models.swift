import Foundation

// MARK: - HUD state (shared across all surfaces)

public enum HUDState: Equatable {
    case idle
    case listening
    case thinking
    case text(title: String, body: String)
    case image(urlString: String)
    case video(urlString: String)
    case error(message: String)
}

// MARK: - Request types

public struct TextQueryRequest: Encodable {
    public let text: String
    public let source: String

    public init(text: String, source: String = "mobile") {
        self.text = text
        self.source = source
    }
}

public struct ImageQueryRequest: Encodable {
    public let imageB64: String
    public let text: String
    public let source: String = "mobile"

    public init(imageB64: String, text: String) {
        self.imageB64 = imageB64
        self.text = text
    }

    enum CodingKeys: String, CodingKey {
        case imageB64 = "image_b64"
        case text
        case source
    }
}

public struct HeartbeatRequest: Encodable {
    public let surfaceId: String
    public let type: String
    public let capabilities: [String]

    public init(surfaceId: String, type: String, capabilities: [String]) {
        self.surfaceId = surfaceId
        self.type = type
        self.capabilities = capabilities
    }

    enum CodingKeys: String, CodingKey {
        case surfaceId = "surface_id"
        case type
        case capabilities
    }
}

// MARK: - Response types

public struct QueryResponse: Decodable {
    public let text: String
    public let audioB64: String
    public let display: DisplayPayload

    enum CodingKeys: String, CodingKey {
        case text
        case audioB64 = "audio_b64"
        case display
    }
}

public struct DisplayPayload: Decodable {
    public enum PayloadType: String, Decodable {
        case text
        case image
        case video
        case audioOnly = "audio_only"
    }

    public let type: PayloadType
    public let title: String
    public let body: String
    public let mediaURL: String?

    enum CodingKeys: String, CodingKey {
        case type
        case title
        case body
        case mediaURL = "media_url"
    }

    public var hudState: HUDState {
        switch type {
        case .text:
            return .text(title: title, body: body)
        case .image:
            guard let url = mediaURL else { return .text(title: title, body: body) }
            return .image(urlString: url)
        case .video:
            guard let url = mediaURL else { return .text(title: title, body: body) }
            return .video(urlString: url)
        case .audioOnly:
            return .idle
        }
    }
}

// MARK: - Chat message model

public struct ChatMessage: Identifiable, Equatable {
    public enum Role: Equatable { case user, jarvis }

    public let id = UUID()
    public let role: Role
    public var text: String
    public var isLoading: Bool

    public init(role: Role, text: String, isLoading: Bool = false) {
        self.role = role
        self.text = text
        self.isLoading = isLoading
    }
}
