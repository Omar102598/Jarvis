import AppIntents
import Foundation

// "Hey Siri, ask Jarvis [anything]" — runs in background or foreground,
// returns the spoken response through Siri without opening the app.
struct AskJarvisIntent: AppIntent {
    static let title: LocalizedStringResource = "Ask Jarvis"
    static let description = IntentDescription(
        "Ask Jarvis anything and hear the response through Siri.",
        categoryName: "Jarvis"
    )

    // Always route every query through the Jarvis engine in the background —
    // never hand off to Siri's native answering or open the app. This keeps a
    // single consistent brain across surfaces (Month 4c).
    static let openAppWhenRun = false

    // Hands-free from AirPods with the phone locked in a pocket: without this,
    // Siri demands an unlock before running the intent.
    static let authenticationPolicy: IntentAuthenticationPolicy = .alwaysAllowed

    // Siri extracts the question from voice input automatically
    @Parameter(title: "Question", requestValueDialog: IntentDialog("What would you like to ask Jarvis?"))
    var question: String

    static var parameterSummary: some ParameterSummary {
        Summary("Ask Jarvis \(\.$question)")
    }

    // Dialog-only on purpose: adding ReturnsValue to THIS intent broke Siri's
    // voice parameter prompt (mic opened then dismissed instantly). The
    // chainable value-returning variant lives in AskJarvisTextIntent below.
    func perform() async throws -> some IntentResult & ProvidesDialog {
        // If Siri's silence detection misfires and hands us an empty question,
        // re-prompt instead of erroring out (an error dismisses the session).
        let trimmed = question.trimmingCharacters(in: .whitespacesAndNewlines)
        guard !trimmed.isEmpty else {
            throw $question.needsValueError(
                IntentDialog("What would you like to ask Jarvis?")
            )
        }
        // Siri gives up silently on long waits — race the backend against a
        // 25s budget and bow out gracefully instead (the backend keeps going;
        // the answer lands in the app's chat via history).
        let response = try await withThrowingTaskGroup(of: String.self) { group in
            group.addTask { try await JarvisClient.shared.askTextForSiri(trimmed) }
            group.addTask {
                try await Task.sleep(nanoseconds: 25_000_000_000)
                return "Still working on that, sir — the full answer will be in the Jarvis app in a moment."
            }
            let first = try await group.next() ?? "Something went wrong, sir."
            group.cancelAll()
            return first
        }
        return .result(dialog: IntentDialog(stringLiteral: response))
    }
}

// Shortcuts-chainable variant — returns the answer as a value so a Shortcut
// can pipe it onward (the "Hey Jarvis" Vocal Shortcut recipe:
// Dictate Text → Ask Jarvis (Text) → Speak Text). Not phrase-registered;
// it appears only as an action in the Shortcuts editor.
struct AskJarvisTextIntent: AppIntent {
    static let title: LocalizedStringResource = "Ask Jarvis (Text)"
    static let description = IntentDescription(
        "Ask Jarvis and get the answer back as text for use in Shortcuts.",
        categoryName: "Jarvis"
    )
    static let openAppWhenRun = false
    static let authenticationPolicy: IntentAuthenticationPolicy = .alwaysAllowed

    @Parameter(title: "Question")
    var question: String

    static var parameterSummary: some ParameterSummary {
        Summary("Ask Jarvis \(\.$question)")
    }

    func perform() async throws -> some IntentResult & ReturnsValue<String> {
        let response = try await JarvisClient.shared.askTextForSiri(question)
        return .result(value: response)
    }
}

// Shortcut app integration — shows up in the Shortcuts library
struct JarvisShortcuts: AppShortcutsProvider {
    static var appShortcuts: [AppShortcut] {
        AppShortcut(
            intent: AskJarvisIntent(),
            phrases: [
                // Siri matches these fairly literally — cover the natural
                // variants. A bare "Ask Jarvis" must be its own phrase.
                "Ask \(.applicationName)",
                "Ask \(.applicationName) something",
                "Ask \(.applicationName) a question",
                "Hey \(.applicationName)",
                "Talk to \(.applicationName)",
            ],
            shortTitle: "Ask Jarvis",
            systemImageName: "sparkles"
        )
    }
}
