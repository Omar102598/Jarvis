import AppIntents
import UIKit

/// "Jarvis Car Greeting": for a Shortcuts automation, Automation → CarPlay →
/// Connects → Run Immediately. Jarvis greets you in his own voice through the
/// car speakers (time of day, plus your next event if it's soon). Works
/// without the CarPlay app entitlement, since Shortcuts does the triggering.
struct CarGreetingIntent: AppIntent {
    static let title: LocalizedStringResource = "Jarvis Car Greeting"
    static let description = IntentDescription(
        "Jarvis greets you when you get in the car. Use it in a Shortcuts automation that runs when CarPlay connects.",
        categoryName: "Jarvis"
    )
    static let openAppWhenRun = false
    static let authenticationPolicy: IntentAuthenticationPolicy = .alwaysAllowed

    @MainActor
    func perform() async throws -> some IntentResult {
        // Return at once and greet in the background. Waiting for the brain,
        // the voice and the whole playback outlasted Shortcuts' time limit, so
        // the automation reported "unknown error" even though the greeting
        // played. The background task keeps the app awake for the network
        // fetch; the audio background mode covers playback. No dialog is
        // returned: Siri would read the greeting a second time in her voice.
        let app = UIApplication.shared
        var bgTask: UIBackgroundTaskIdentifier = .invalid
        bgTask = app.beginBackgroundTask(withName: "CarGreeting") {
            app.endBackgroundTask(bgTask)
            bgTask = .invalid
        }
        Task { @MainActor in
            await CarGreeting.run()
            if bgTask != .invalid {
                app.endBackgroundTask(bgTask)
                bgTask = .invalid
            }
        }
        return .result()
    }
}
