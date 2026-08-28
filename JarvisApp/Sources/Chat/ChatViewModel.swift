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
        let msg = ChatMessage(role: .jarvis, text: text, mediaURL: payload.mediaURL)
        messages.append(msg)
        pinSnapshotBytes(for: msg.id, urlString: payload.mediaURL)
    }

    /// Snapshot (not a video stream): download the bytes once and pin them
    /// to the message so it renders from memory — never re-fetches, never
    /// flashes-then-vanishes on re-render.
    private func pinSnapshotBytes(for id: UUID, urlString: String?) {
        guard let m = urlString, !m.contains(".m3u8"), let url = URL(string: m) else { return }
        Task { [weak self] in
            guard let data = try? await URLSession.shared.data(from: url).0 else { return }
            await MainActor.run {
                if let idx = self?.messages.firstIndex(where: { $0.id == id }) {
                    self?.messages[idx].imageData = data
                }
            }
        }
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
    /// the persisted push feed, then rebuild the local chat. Pushes (Sentry
    /// cards, agent reports) are NOT in the conversation history and the
    /// WebSocket only reaches a foregrounded app — the /pushes feed is how
    /// cards sent while the app was suspended finally appear. Skipped
    /// mid-request so an in-flight loading bubble isn't clobbered.
    func refreshHistory() async {
        guard !isProcessing else { return }
        guard let history = try? await JarvisClient.shared.fetchHistory() else { return }
        let pushes = (try? await JarvisClient.shared.fetchPushes()) ?? []
        let fresh = history.map {
            ChatMessage(role: $0.role == "user" ? .user : .jarvis, text: $0.text)
        }
        guard !fresh.isEmpty || !pushes.isEmpty else { return }

        // Carry cards already in the chat (live WS arrivals have no pushID;
        // previously merged feed items keep theirs, so bytes aren't re-fetched).
        let carried = messages.filter { $0.mediaURL != nil || $0.pushID != nil }
        var merged = fresh + carried

        for item in pushes {
            let isDupe = merged.contains {
                $0.pushID == item.id
                    || ($0.mediaURL != nil && $0.mediaURL == item.mediaURL)
                    || ($0.mediaURL == nil && item.mediaURL == nil && $0.text == item.text)
            }
            if isDupe { continue }
            var msg = ChatMessage(role: .jarvis, text: item.text, mediaURL: item.mediaURL)
            msg.pushID = item.id
            merged.append(msg)
        }

        if merged.count != messages.count || merged.last?.text != messages.last?.text {
            messages = merged
            for msg in merged where msg.mediaURL != nil && msg.imageData == nil {
                pinSnapshotBytes(for: msg.id, urlString: msg.mediaURL)
            }
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
            // Typed in, so reply in text only — no synthesis requested, nothing
            // to play. Holding the mic button (sendVoice) still speaks.
            let response = try await JarvisClient.shared.askText(text, speak: false)
            let displayText = response.display.body.isEmpty ? response.text : response.display.body
            finishLoading(loadingId, text: displayText)
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
            // Camera capture means eyes already on the screen — show, don't speak.
            let response = try await JarvisClient.shared.askImage(imageData, text: prompt)
            finishLoading(loadingId, text: response.text)
            await glassesManager?.send(response.display.hudState)
        } catch {
            finishLoading(loadingId, text: "Error: \(error.localizedDescription)")
            await glassesManager?.send(.error(message: error.localizedDescription))
        }
    }

    // MARK: Video query (phone camera roll)

    func sendVideo(_ videoData: Data, prompt: String = "What is happening in this video?") async {
        let mb = Double(videoData.count) / 1_048_576.0
        guard mb <= 40 else {
            append(.jarvis, text: "That clip is \(String(format: "%.0f", mb))MB — too large to send. "
                                   + "Try a shorter clip (5-20s works best).")
            return
        }
        append(.user, text: "\(prompt) (video, \(String(format: "%.1f", mb))MB)")
        let loadingId = appendLoading()
        await glassesManager?.send(.thinking)
        isProcessing = true
        defer { isProcessing = false }

        do {
            let response = try await JarvisClient.shared.askVideo(videoData, text: prompt)
            let displayText = response.display.body.isEmpty ? response.text : response.display.body
            finishLoading(loadingId, text: displayText)
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
