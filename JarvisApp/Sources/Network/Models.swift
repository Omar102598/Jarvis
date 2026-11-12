import Foundation

// MARK: - Request types

struct TextQueryRequest: Encodable {
    let text: String
    var source: String = "app"
    /// Modality matching: typed messages get a typed reply, so the server skips
    /// synthesis (saves TTS spend and latency). Voice turns keep speaking.
    var speak: Bool = true
}

struct VideoQueryRequest: Encodable {
    let videoB64: String
    let text: String
    var source: String = "app"
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
    var source: String = "app"

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
    // Optional so the app can say "nothing upcoming" explicitly. Staying silent
    // was indistinguishable from a failed sync, and the backend key had no TTL,
    // so the last event pushed stayed there indefinitely.
    let title: String?
    let start: String?       // ISO-8601
    let location: String?

    static let none = NextCalendarEvent(title: nil, start: nil, location: nil)
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
    /// Native cards this turn's tools attached (macro tracker, tables, charts,
    /// one-tap actions). Empty on the glasses surface and older gateways.
    let cards: [ChatCard]

    enum CodingKeys: String, CodingKey {
        case text
        case audioB64 = "audio_b64"
        case display
        case turnID = "turn_id"
        case cards
    }

    init(from decoder: Decoder) throws {
        let c = try decoder.container(keyedBy: CodingKeys.self)
        text     = try c.decodeIfPresent(String.self, forKey: .text) ?? ""
        audioB64 = try c.decodeIfPresent(String.self, forKey: .audioB64) ?? ""
        display  = try c.decode(DisplayPayload.self, forKey: .display)
        // Absent on the voice path and on any gateway older than this change.
        turnID   = try c.decodeIfPresent(String.self, forKey: .turnID) ?? ""
        cards    = (try? c.decodeIfPresent([ChatCard].self, forKey: .cards)) ?? []
    }
}

// MARK: - Rich chat cards
//
// Built server-side by TOOLS from real data (services/llm_agent/tools/cards.py),
// never written by the model, then rendered natively by ChatCardView. One
// flexible struct rather than an enum of payloads: every field is optional, so
// a card type the app doesn't know yet decodes fine and is simply skipped.

struct ChatCard: Decodable, Equatable, Identifiable {
    let id = UUID()
    let type: String
    let title: String
    let subtitle: String
    let footer: String
    // macros
    let calories: MacroValue?
    let bars: [MacroBar]
    let meals: [String]
    // stats
    let items: [StatItem]
    // table
    let columns: [String]
    let rows: [[String]]
    // chart
    let kind: String
    let unit: String
    let points: [ChartPoint]
    let target: Double
    // action
    let icon: String
    let approvalID: String
    let status: String

    struct MacroValue: Decodable, Equatable { let value: Double; let target: Double }
    struct MacroBar: Decodable, Equatable {
        let label: String; let value: Double; let target: Double
        let unit: String; let color: String
    }
    struct StatItem: Decodable, Equatable { let label: String; let value: String; let sub: String
        init(from d: Decoder) throws {
            let c = try d.container(keyedBy: K.self)
            label = (try? c.decode(String.self, forKey: .label)) ?? ""
            value = ChatCard.flexString(c, .value)
            sub   = (try? c.decode(String.self, forKey: .sub)) ?? ""
        }
        enum K: String, CodingKey { case label, value, sub }
    }
    struct ChartPoint: Decodable, Equatable, Identifiable {
        var id: String { x }
        let x: String; let y: Double
    }

    enum CodingKeys: String, CodingKey {
        case type, title, subtitle, footer, calories, bars, meals, items, columns, rows
        case kind, unit, points, target, icon, status
        case approvalID = "approval_id"
    }

    init(from d: Decoder) throws {
        let c = try d.container(keyedBy: CodingKeys.self)
        type     = (try? c.decode(String.self, forKey: .type)) ?? ""
        title    = (try? c.decode(String.self, forKey: .title)) ?? ""
        subtitle = (try? c.decode(String.self, forKey: .subtitle)) ?? ""
        footer   = (try? c.decode(String.self, forKey: .footer)) ?? ""
        calories = try? c.decode(MacroValue.self, forKey: .calories)
        bars     = (try? c.decode([MacroBar].self, forKey: .bars)) ?? []
        meals    = (try? c.decode([String].self, forKey: .meals)) ?? []
        items    = (try? c.decode([StatItem].self, forKey: .items)) ?? []
        columns  = (try? c.decode([String].self, forKey: .columns)) ?? []
        rows     = (try? c.decode([[String]].self, forKey: .rows)) ?? []
        kind     = (try? c.decode(String.self, forKey: .kind)) ?? "bar"
        unit     = (try? c.decode(String.self, forKey: .unit)) ?? ""
        points   = (try? c.decode([ChartPoint].self, forKey: .points)) ?? []
        target   = (try? c.decode(Double.self, forKey: .target)) ?? 0
        icon     = (try? c.decode(String.self, forKey: .icon)) ?? "sparkles"
        approvalID = (try? c.decode(String.self, forKey: .approvalID)) ?? ""
        status   = (try? c.decode(String.self, forKey: .status)) ?? ""
    }

    /// A value that may arrive as a string or a number ("205g" or 205).
    static func flexString<K: CodingKey>(_ c: KeyedDecodingContainer<K>, _ key: K) -> String {
        if let s = try? c.decode(String.self, forKey: key) { return s }
        if let i = try? c.decode(Int.self, forKey: key) { return String(i) }
        if let f = try? c.decode(Double.self, forKey: key) { return String(f) }
        return ""
    }

    static func == (a: ChatCard, b: ChatCard) -> Bool { a.id == b.id }
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
    /// The user's attachment was a video (imageData is its first frame).
    var mediaIsVideo: Bool = false
    /// Server-side push id (gateway /pushes feed) — dedupes the merge between
    /// live WebSocket cards and the persisted feed fetched on foreground.
    var pushID: String? = nil
    /// Tool calls made while producing this reply, in call order. Rendered
    /// inline under the message the way Claude/ChatGPT show their work.
    var toolCalls: [ToolEvent] = []
    /// The turn that produced this message, once known. Lets tool calls be
    /// matched to it exactly, and backfilled if the live stream missed any.
    var turnID: String? = nil
    /// Native cards attached by this turn's tools, rendered above the text.
    var cards: [ChatCard] = []
    /// When the server recorded it (history ts / push ts). Used to interleave
    /// turns and proactive pushes in time order on refresh. nil = unknown.
    var date: Date? = nil

    static func parseDate(_ s: String) -> Date? {
        guard !s.isEmpty else { return nil }
        let f = ISO8601DateFormatter()
        f.formatOptions = [.withInternetDateTime, .withFractionalSeconds]
        return f.date(from: s) ?? ISO8601DateFormatter().date(from: s)
    }
}

// MARK: - Persisted push (gateway /pushes — cards missed while suspended)

struct PushItem: Decodable {
    let id: String
    let title: String
    let text: String
    let mediaURL: String?
    let type: String
    let ts: String
    let cards: [ChatCard]

    enum CodingKeys: String, CodingKey {
        case id, title, text, type, ts, cards
        case mediaURL = "media_url"
    }

    init(from d: Decoder) throws {
        let c = try d.container(keyedBy: CodingKeys.self)
        id       = try c.decode(String.self, forKey: .id)
        title    = (try? c.decode(String.self, forKey: .title)) ?? "Jarvis"
        text     = (try? c.decode(String.self, forKey: .text)) ?? ""
        mediaURL = try? c.decode(String.self, forKey: .mediaURL)
        type     = (try? c.decode(String.self, forKey: .type)) ?? "text"
        ts       = (try? c.decode(String.self, forKey: .ts)) ?? ""
        cards    = (try? c.decode([ChatCard].self, forKey: .cards)) ?? []
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
