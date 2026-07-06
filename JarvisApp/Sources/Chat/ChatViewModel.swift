import Foundation
import AVFoundation
import UIKit
import UserNotifications

@MainActor
final class ChatViewModel: ObservableObject {
    @Published var messages: [ChatMessage] = []
    @Published var isProcessing = false
    @Published var errorMessage: String?
    @Published var isPlayingAudio = false
    @Published var toolEvents: [ToolEvent] = []

    private let recorder = VoiceRecorder()
    private var audioPlayer: AVAudioPlayer?
    private var audioDelegate: AudioDelegate?
    private let socket = JarvisSocket()
    private var toolPollingTask: Task<Void, Never>?

    weak var glassesManager: GlassesManager?

    // MARK: Tool event polling

    func startToolPolling() {
        toolPollingTask?.cancel()
        toolPollingTask = Task { [weak self] in
            while !Task.isCancelled {
                guard let self else { return }
                if let events = try? await JarvisClient.shared.fetchToolEvents() {
                    self.toolEvents = events
                }
                // Fast while Jarvis is working (live timeline), slow when idle
                // so the app isn't hammering the battery 24/7.
                let interval: UInt64 = self.isProcessing ? 2_500_000_000 : 20_000_000_000
                try? await Task.sleep(nanoseconds: interval)
            }
        }
    }

    // MARK: WebSocket (auto-reconnecting)

    func connectWebSocket() async {
        socket.start { [weak self] payload in
            Task { @MainActor in
                await self?.glassesManager?.send(payload.hudState)
                self?.notifyIfBackgrounded(payload)
                self?.appendPush(payload)
            }
        }
    }

    /// Proactive pushes (Sentry alerts, live views, agent reports) appear in
    /// the chat as cards — tap to view snapshots full-screen / play streams.
    private func appendPush(_ payload: DisplayPayload) {
        guard payload.type != .audioOnly else { return }
        let text = payload.body.isEmpty ? payload.title : payload.body
        guard !text.isEmpty else { return }
        messages.append(ChatMessage(role: .jarvis, text: text,
                                    mediaURL: payload.mediaURL))
    }

    // MARK: Local notifications — surface pushes that arrive while backgrounded

    func requestNotificationPermission() {
        UNUserNotificationCenter.current().requestAuthorization(
            options: [.alert, .sound, .badge]
        ) { _, _ in }
    }

    private func notifyIfBackgrounded(_ payload: DisplayPayload) {
        guard UIApplication.shared.applicationState != .active else { return }
        let body = payload.body.isEmpty ? payload.title : payload.body
        guard !body.isEmpty else { return }
        let content = UNMutableNotificationContent()
        content.title = payload.title.isEmpty ? "Jarvis" : payload.title
        content.body = String(body.prefix(200))
        content.sound = .default
        UNUserNotificationCenter.current().add(
            UNNotificationRequest(identifier: UUID().uuidString, content: content, trigger: nil)
        )
    }

    // MARK: Conversation history — restore on launch, refresh on foreground

    func loadHistory() async {
        guard messages.isEmpty else { return }
        await refreshHistory()
    }

    /// Pull the shared backend history (other surfaces' turns included) and
    /// replace the local chat if it changed. Skipped mid-request so an
    /// in-flight loading bubble isn't clobbered.
    func refreshHistory() async {
        guard !isProcessing else { return }
        guard let history = try? await JarvisClient.shared.fetchHistory() else { return }
        let fresh = history.map {
            ChatMessage(role: $0.role == "user" ? .user : .jarvis, text: $0.text)
        }
        guard !fresh.isEmpty else { return }
        if fresh.count != messages.count || fresh.last?.text != messages.last?.text {
            // Media cards (Sentry snapshots, live views) arrive via WebSocket
            // pushes and are NOT in the backend conversation history — carry
            // them across the replace or they silently vanish on foreground.
            let mediaCards = messages.filter { $0.mediaURL != nil }
            messages = fresh + mediaCards
        }
    }

    // MARK: Text query

    func sendText(_ text: String) async {
        guard !text.trimmingCharacters(in: .whitespaces).isEmpty else { return }
        append(.user, text: text)
        let loadingId = appendLoading()
        await glassesManager?.send(.thinking)
        isProcessing = true
        defer { isProcessing = false }

        do {
            let response = try await JarvisClient.shared.askText(text)
            let displayText = response.display.body.isEmpty ? response.text : response.display.body
            finishLoading(loadingId, text: displayText)
            playAudio(base64: response.audioB64)
            await glassesManager?.send(response.display.hudState)
        } catch {
            finishLoading(loadingId, text: "Error: \(error.localizedDescription)")
            await glassesManager?.send(.error(message: error.localizedDescription))
        }
    }

    // MARK: Voice query (from PTT button)

    func sendVoice() async {
        isProcessing = true
        defer { isProcessing = false }
        await glassesManager?.send(.listening)

        do {
            let audioData = try await recorder.record()
            let userPlaceholder = appendLoading()
            let loadingId = appendLoading()
            await glassesManager?.send(.thinking)

            let response = try await JarvisClient.shared.askAudio(audioData)
            finishLoading(userPlaceholder, text: "(voice)")
            let displayText = response.display.body.isEmpty ? response.text : response.display.body
            finishLoading(loadingId, text: displayText)
            playAudio(base64: response.audioB64)
            await glassesManager?.send(response.display.hudState)
        } catch {
            errorMessage = error.localizedDescription
            await glassesManager?.send(.idle)
        }
    }

    // MARK: Image query (from camera button)

    func sendImage(_ imageData: Data, prompt: String = "What am I looking at?") async {
        append(.user, text: prompt)
        let loadingId = appendLoading()
        await glassesManager?.send(.thinking)
        isProcessing = true
        defer { isProcessing = false }

        do {
            let response = try await JarvisClient.shared.askImage(imageData, text: prompt)
            finishLoading(loadingId, text: response.text)
            playAudio(base64: response.audioB64)
            await glassesManager?.send(response.display.hudState)
        } catch {
            finishLoading(loadingId, text: "Error: \(error.localizedDescription)")
            await glassesManager?.send(.error(message: error.localizedDescription))
        }
    }

    // MARK: Audio playback

    private func playAudio(base64: String) {
        guard !base64.isEmpty,
              let data = Data(base64Encoded: base64) else { return }
        do {
            try AVAudioSession.sharedInstance().setCategory(.playback)
            try AVAudioSession.sharedInstance().setActive(true)
            audioPlayer = try AVAudioPlayer(data: data)
            let delegate = AudioDelegate { [weak self] in
                Task { @MainActor in self?.isPlayingAudio = false }
            }
            audioDelegate = delegate
            audioPlayer?.delegate = delegate
            audioPlayer?.play()
            isPlayingAudio = true
        } catch {
            print("[ChatViewModel] Audio playback failed: \(error)")
        }
    }

    // MARK: Message helpers

    private func append(_ role: ChatMessage.Role, text: String) {
        messages.append(ChatMessage(role: role, text: text))
    }

    private func appendLoading() -> UUID {
        let msg = ChatMessage(role: .jarvis, text: "", isLoading: true)
        messages.append(msg)
        return msg.id
    }

    private func finishLoading(_ id: UUID, text: String) {
        if let idx = messages.firstIndex(where: { $0.id == id }) {
            messages[idx].text = text
            messages[idx].isLoading = false
        }
    }
}

// MARK: - AVAudioPlayerDelegate bridge (not MainActor-isolated)

private final class AudioDelegate: NSObject, AVAudioPlayerDelegate {
    private let onFinish: () -> Void
    init(onFinish: @escaping () -> Void) { self.onFinish = onFinish }
    func audioPlayerDidFinishPlaying(_ player: AVAudioPlayer, successfully flag: Bool) {
        onFinish()
    }
}
