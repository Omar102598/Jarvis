import Foundation

// MARK: - Request types

struct TextQueryRequest: Encodable {
    let text: String
    let source: String = "glasses"
    /// Modality matching: typed messages get a typed reply, so the server skips
    /// synthesis (saves TTS spend and latency). Voice turns keep speaking.
    var speak: Bool = true
}

struct VideoQueryRequest: Encodable {
    let videoB64: String
    let text: String
    let source: String = "glasses"
    /// Recording a clip means the screen is already in hand — reply in text.
    var speak: Bool = false

    enum CodingKeys: String, CodingKey {
        case videoB64 = "video_b64"
        case text, source, speak
    }
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
    /// Identifies the request that produced this reply. The brain stamps every
    /// tool event of the turn with it, so the client can attach exactly this
    /// turn's tool calls to this message rather than guessing from timing.
    let turnID: String

    enum CodingKeys: String, CodingKey {
        case text
        case audioB64 = "audio_b64"
        case display
        case turnID = "turn_id"
    }

    init(from decoder: Decoder) throws {
        let c = try decoder.container(keyedBy: CodingKeys.self)
        text     = try c.decodeIfPresent(String.self, forKey: .text) ?? ""
        audioB64 = try c.decodeIfPresent(String.self, forKey: .audioB64) ?? ""
        display  = try c.decode(DisplayPayload.self, forKey: .display)
        // Absent on the voice path and on any gateway older than this change.
        turnID   = try c.decodeIfPresent(String.self, forKey: .turnID) ?? ""
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
    /// Snapshot or HLS stream attached to the message (Ring alerts, live view)
    var mediaURL: String? = nil
    /// Snapshot bytes downloaded ONCE when the card arrived, held in memory so
    /// the image renders from data and never re-fetches (which was causing
    /// snapshots to flash then vanish on list re-render). Videos stay URL-based.
    var imageData: Data? = nil
    /// Server-side push id (gateway /pushes feed) — dedupes the merge between
    /// live WebSocket cards and the persisted feed fetched on foreground.
    var pushID: String? = nil
    /// Tool calls made while producing this reply, in call order. Rendered
    /// inline under the message the way Claude/ChatGPT show their work.
    var toolCalls: [ToolEvent] = []
    /// The turn that produced this message, once known. Lets tool calls be
    /// matched to it exactly, and backfilled if the live stream missed any.
    var turnID: String? = nil
}

// MARK: - Persisted push (gateway /pushes — cards missed while suspended)

struct PushItem: Decodable {
    let id: String
    let title: String
    let text: String
    let mediaURL: String?
    let type: String
    let ts: String

    enum CodingKeys: String, CodingKey {
        case id, title, text, type, ts
        case mediaURL = "media_url"
    }
}

// MARK: - Tool event model

struct ToolEvent: Identifiable, Decodable, Equatable {
    let id: String
    let tool: String
    let argsPreview: String
    let status: String
    let resultPreview: String
    let timestamp: String
    /// The model's own tool-call id. Pairs a "calling" event with the "done"
    /// event that answers it, so the UI shows ONE row that resolves rather than
    /// two unrelated rows. Empty on events from before this existed.
    let callID: String
    /// Groups every tool call from a single request, so calls render under the
    /// message that caused them instead of in one flat global list.
    let turnID: String

    enum CodingKeys: String, CodingKey {
        case id, tool, status, timestamp
        case argsPreview   = "args_preview"
        case resultPreview = "result_preview"
        case callID        = "call_id"
        case turnID        = "turn_id"
    }

    init(from decoder: Decoder) throws {
        let c = try decoder.container(keyedBy: CodingKeys.self)
        id            = try c.decodeIfPresent(String.self, forKey: .id) ?? UUID().uuidString
        tool          = try c.decodeIfPresent(String.self, forKey: .tool) ?? "unknown"
        argsPreview   = try c.decodeIfPresent(String.self, forKey: .argsPreview) ?? ""
        status        = try c.decodeIfPresent(String.self, forKey: .status) ?? ""
        resultPreview = try c.decodeIfPresent(String.self, forKey: .resultPreview) ?? ""
        timestamp     = try c.decodeIfPresent(String.self, forKey: .timestamp) ?? ""
        // Older persisted events predate correlation — decode them rather than
        // failing the whole batch, and let them fall back to flat display.
        callID        = try c.decodeIfPresent(String.self, forKey: .callID) ?? ""
        turnID        = try c.decodeIfPresent(String.self, forKey: .turnID) ?? ""
    }

    /// True once the call has returned — drives the spinner/checkmark.
    var isFinished: Bool { status == "done" }
}

struct ToolEventsResponse: Decodable {
    let events: [ToolEvent]
}
