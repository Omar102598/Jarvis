import CarPlay
import UIKit

/// Jarvis on the car screen: a voice-control template showing the arc, which
/// greets you when CarPlay connects, then listens, thinks and replies.
///
/// Needs Apple's com.apple.developer.carplay-voice-based-conversation
/// entitlement (iOS 26.4+, paid developer account) to appear in a real car;
/// until then it runs in Xcode's CarPlay simulator (I/O → External Displays →
/// CarPlay), where the simulator-only entitlements file grants it.
final class CarPlaySceneDelegate: UIResponder, CPTemplateApplicationSceneDelegate {
    private var interfaceController: CPInterfaceController?
    private var voiceTemplate: CPVoiceControlTemplate?
    private var session: Task<Void, Never>?
    private let recorder = VoiceRecorder(leadInSecs: 8)

    private enum State: String { case greeting, listening, thinking, speaking, idle }

    func templateApplicationScene(_ scene: CPTemplateApplicationScene,
                                  didConnect interfaceController: CPInterfaceController) {
        self.interfaceController = interfaceController
        Task { @MainActor in
            let template = CPVoiceControlTemplate(voiceControlStates: Self.states())
            voiceTemplate = template
            interfaceController.setRootTemplate(template, animated: false, completion: nil)
            start(greet: true)
        }
    }

    func templateApplicationScene(_ scene: CPTemplateApplicationScene,
                                  didDisconnectInterfaceController interfaceController: CPInterfaceController) {
        session?.cancel()
        session = nil
        recorder.stopEarly()
        Task { @MainActor in JarvisSpeaker.shared.stop() }
        self.interfaceController = nil
        voiceTemplate = nil
    }

    /// Tapping the Jarvis icon again after the conversation went idle.
    func sceneDidBecomeActive(_ scene: UIScene) {
        if session == nil, voiceTemplate != nil { Task { @MainActor in start(greet: false) } }
    }

    @MainActor
    private func start(greet: Bool) {
        session?.cancel()
        session = Task { @MainActor [weak self] in
            guard let self else { return }
            if greet {
                show(.greeting)
                let text = await CarGreeting.run()
                if text.isEmpty { show(.idle); session = nil; return }
            }
            // Converse until the driver stops talking.
            while !Task.isCancelled {
                show(.listening)
                guard let wav = try? await recorder.record(), recorder.heardSpeech else { break }
                show(.thinking)
                guard let reply = try? await JarvisClient.shared.askAudio(wav, source: "car") else {
                    break
                }
                show(.speaking)
                await JarvisSpeaker.shared.play(base64: reply.audioB64)
            }
            show(.idle)
            session = nil
        }
    }

    @MainActor
    private func show(_ state: State) {
        voiceTemplate?.activateVoiceControlState(withIdentifier: state.rawValue)
    }

    @MainActor
    private static func states() -> [CPVoiceControlState] {
        func make(_ s: State, _ titles: [String], _ mood: ArcMood) -> CPVoiceControlState {
            CPVoiceControlState(identifier: s.rawValue, titleVariants: titles,
                                image: ArcFrames.animated(mood), repeats: true)
        }
        return [
            make(.greeting, ["Jarvis online"], .speaking),
            make(.listening, ["Listening, sir", "Listening"], .listening),
            make(.thinking, ["Working on it", "Thinking"], .thinking),
            make(.speaking, ["Jarvis"], .speaking),
            make(.idle, ["Tap Jarvis to talk", "Standing by"], .idle),
        ]
    }
}
