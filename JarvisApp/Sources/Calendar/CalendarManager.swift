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
        guard authorized else {
            // Reinstalling the app revokes calendar permission, and this used to
            // return in silence — so the backend kept serving whatever it had
            // last been told, with nothing anywhere indicating why it never
            // changed. Say so at least once.
            print("[Calendar] Not authorized — no events will sync. "
                  + "Grant calendar access in Settings > Jarvis.")
            return
        }
        await pushNextEvent()
    }

    // MARK: - Next event

    func pushNextEvent() async {
        let payload: NextCalendarEvent
        if let event = nextEvent() {
            let iso = ISO8601DateFormatter()
            payload = NextCalendarEvent(
                title: event.title ?? "Untitled event",
                start: iso.string(from: event.startDate),
                location: event.location
            )
        } else {
            // Explicitly "nothing upcoming" rather than returning early. The old
            // silence left the previous event in place forever: with a 24-hour
            // window and no event tomorrow, the backend was never contacted at
            // all, so a six-day-old flight stayed as the answer.
            payload = .none
        }

        do {
            try await JarvisClient.shared.pushNextCalendarEvent(payload)
        } catch {
            print("[Calendar] Push failed: \(error)")
        }
    }

    /// The soonest event starting within the next 24 hours.
    private func nextEvent() -> EKEvent? {
        let now = Date()
        // A week, not a day. The ambient countdown only cares about the next
        // few hours, but "what's on my calendar?" does not — and with a 24-hour
        // window anything further out simply did not exist as far as the
        // backend was concerned.
        let end = now.addingTimeInterval(7 * 24 * 3600)
        let predicate = store.predicateForEvents(withStart: now, end: end, calendars: nil)
        let events = store.events(matching: predicate)
            .filter { !$0.isAllDay && $0.startDate > now }
            .sorted { $0.startDate < $1.startDate }
        return events.first
    }
}
