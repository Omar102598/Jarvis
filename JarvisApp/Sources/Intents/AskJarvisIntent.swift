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

    // Siri extracts the question from voice input automatically
    @Parameter(title: "Question", requestValueDialog: IntentDialog("What would you like to ask Jarvis?"))
    var question: String

    static var parameterSummary: some ParameterSummary {
        Summary("Ask Jarvis \(\.$question)")
    }

    func perform() async throws -> some IntentResult & ProvidesDialog {
        let response = try await JarvisClient.shared.askTextForSiri(question)
        return .result(dialog: IntentDialog(stringLiteral: response))
    }
}

// Shortcut app integration — shows up in the Shortcuts library
struct JarvisShortcuts: AppShortcutsProvider {
    static var appShortcuts: [AppShortcut] {
        AppShortcut(
            intent: AskJarvisIntent(),
            phrases: [
                "Ask \(.applicationName) something",
                "Hey \(.applicationName)",
            ],
            shortTitle: "Ask Jarvis",
            systemImageName: "sparkles"
        )
    }
}
