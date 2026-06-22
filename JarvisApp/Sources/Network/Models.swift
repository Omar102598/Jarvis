import Foundation

// MARK: - Request types

struct TextQueryRequest: Encodable {
    let text: String
    let source: String = "glasses"
}

struct ImageQueryRequest: Encodable {
    let imageB64: String
    let text: String
    let source: String = "glasses"

    enum CodingKeys: String, CodingKey {
        case imageB64 = "image_b64"
        case text
        case source
    }
}

// MARK: - Response types

struct QueryResponse: Decodable {
    let text: String
    let audioB64: String
    let display: DisplayPayload

    enum CodingKeys: String, CodingKey {
        case text
        case audioB64 = "audio_b64"
        case display
    }
}

struct DisplayPayload: Decodable {
    enum PayloadType: String, Decodable {
        case text
        case image
        case video
        case audioOnly = "audio_only"
    }

    let type: PayloadType
    let title: String
    let body: String
    let mediaURL: String?

    enum CodingKeys: String, CodingKey {
        case type
        case title
        case body
        case mediaURL = "media_url"
    }

    var hudState: HUDState {
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

struct ChatMessage: Identifiable, Equatable {
    enum Role { case user, jarvis }

    let id = UUID()
    let role: Role
    var text: String
    var isLoading: Bool = false
}
