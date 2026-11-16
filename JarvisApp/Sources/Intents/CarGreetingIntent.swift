import AppIntents

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

    func perform() async throws -> some IntentResult {
        // The spoken reply is the greeting; returning a dialog too would make
        // Siri read it a second time in her own voice.
        await CarGreeting.run()
        return .result()
    }
}
