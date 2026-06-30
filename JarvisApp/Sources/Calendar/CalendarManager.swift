import Foundation
import EventKit

/// Reads the user's next upcoming calendar event via EventKit and pushes it to
/// the Jarvis backend, where the ambient agent uses it to warn the user before
/// the event starts (the backend writes it to `jarvis:calendar:next_event`).
///
/// Read-only: Jarvis never creates or edits calendar events.
@MainActor
final class CalendarManager: ObservableObject {
    static let shared = CalendarManager()

    private let store = EKEventStore()

    @Published private(set) var authorized = false

    private init() {}

    // MARK: - Authorization

    func requestAuthorization() async {
        do {
            if #available(iOS 17.0, *) {
                authorized = try await store.requestFullAccessToEvents()
            } else {
                authorized = try await store.requestAccess(to: .event)
            }
        } catch {
            print("[Calendar] Authorization failed: \(error)")
            authorized = false
        }
    }

    /// Request access (if needed) and push the next event. Safe to call on every
    /// app foreground.
    func syncOnLaunch() async {
        if !authorized { await requestAuthorization() }
        guard authorized else { return }
        await pushNextEvent()
    }

    // MARK: - Next event

    func pushNextEvent() async {
        guard let event = nextEvent() else { return }

        let iso = ISO8601DateFormatter()
        let payload = NextCalendarEvent(
            title: event.title ?? "Untitled event",
            start: iso.string(from: event.startDate),
            location: event.location
        )

        do {
            try await JarvisClient.shared.pushNextCalendarEvent(payload)
        } catch {
            print("[Calendar] Push failed: \(error)")
        }
    }

    /// The soonest event starting within the next 24 hours.
    private func nextEvent() -> EKEvent? {
        let now = Date()
        let end = now.addingTimeInterval(24 * 3600)
        let predicate = store.predicateForEvents(withStart: now, end: end, calendars: nil)
        let events = store.events(matching: predicate)
            .filter { !$0.isAllDay && $0.startDate > now }
            .sorted { $0.startDate < $1.startDate }
        return events.first
    }
}
