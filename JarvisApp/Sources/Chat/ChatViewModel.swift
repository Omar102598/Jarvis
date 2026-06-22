import Foundation
import AVFoundation

@MainActor
final class ChatViewModel: ObservableObject {
    @Published var messages: [ChatMessage] = []
    @Published var isProcessing = false
    @Published var errorMessage: String?

    private let recorder = VoiceRecorder()
    private var audioPlayer: AVAudioPlayer?
    private var webSocketTask: URLSessionWebSocketTask?

    weak var glassesManager: GlassesManager?

    // MARK: WebSocket

    func connectWebSocket() async {
        webSocketTask?.cancel()
        webSocketTask = JarvisClient.shared.connectWebSocket { [weak self] payload in
            Task { @MainActor in
                self?.glassesManager?.send(payload.hudState)
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
            let response = try await JarvisClient.shared.askText(text)
            finishLoading(loadingId, text: response.text)
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
            finishLoading(loadingId, text: response.text)
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
            audioPlayer?.play()
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
