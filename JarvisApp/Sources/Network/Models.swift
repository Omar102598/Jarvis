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

// MARK: - HealthKit snapshot

/// A daily snapshot of fitness metrics read from HealthKit and pushed to the
/// backend. All fields optional — only what HealthKit grants/returns is sent.
struct HealthSnapshot: Encodable {
    var steps: Double?
    var activeEnergyKcal: Double?
    var restingHeartRate: Double?
    var hrvMs: Double?               // heart rate variability (SDNN, ms)
    var sleepHours: Double?
    var bodyMassLbs: Double?
    var workoutsToday: Int?
    var workoutMinutesToday: Double?
    let source: String = "ios_healthkit"

    enum CodingKeys: String, CodingKey {
        case steps
        case activeEnergyKcal     = "active_energy_kcal"
        case restingHeartRate     = "resting_heart_rate"
        case hrvMs                = "hrv_ms"
        case sleepHours           = "sleep_hours"
        case bodyMassLbs          = "body_mass_lbs"
        case workoutsToday        = "workouts_today"
        case workoutMinutesToday  = "workout_minutes_today"
        case source
    }
}

// MARK: - Calendar event

/// The next upcoming calendar event, pushed so the ambient agent can warn the
/// user before it starts.
struct NextCalendarEvent: Encodable {
    let title: String
    let start: String        // ISO-8601
    let location: String?
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

// MARK: - Tool event model

struct ToolEvent: Identifiable, Decodable {
    let id: String
    let tool: String
    let argsPreview: String
    let status: String
    let resultPreview: String
    let timestamp: String

    enum CodingKeys: String, CodingKey {
        case id, tool, status, timestamp
        case argsPreview   = "args_preview"
        case resultPreview = "result_preview"
    }
}

struct ToolEventsResponse: Decodable {
    let events: [ToolEvent]
}
