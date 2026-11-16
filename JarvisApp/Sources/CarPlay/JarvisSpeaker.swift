import AVFoundation

/// Plays one Jarvis reply (the gateway's ElevenLabs audio) and returns when it
/// has finished, so the car loop and the greeting intent can wait on it. In a
/// car the audio session routes to the car speakers on its own.
@MainActor
final class JarvisSpeaker: NSObject, AVAudioPlayerDelegate {
    static let shared = JarvisSpeaker()
    private var player: AVAudioPlayer?
    private var done: CheckedContinuation<Void, Never>?

    func play(base64: String) async {
        guard !base64.isEmpty, let data = Data(base64Encoded: base64) else { return }
        stop()
        do {
            // .spokenAudio + duckOthers lowers the car's music under Jarvis
            // instead of stopping it.
            try AVAudioSession.sharedInstance().setCategory(
                .playback, mode: .spokenAudio, options: [.duckOthers])
            try AVAudioSession.sharedInstance().setActive(true)
            let p = try AVAudioPlayer(data: data)
            p.delegate = self
            player = p
            await withCheckedContinuation { cont in
                done = cont
                if !p.play() { finish() }
            }
            try? AVAudioSession.sharedInstance().setActive(false, options: [.notifyOthersOnDeactivation])
        } catch {
            print("[JarvisSpeaker] playback failed: \(error)")
        }
    }

    func stop() {
        player?.stop()
        finish()
    }

    private func finish() {
        player = nil
        done?.resume()
        done = nil
    }

    nonisolated func audioPlayerDidFinishPlaying(_ player: AVAudioPlayer, successfully flag: Bool) {
        Task { @MainActor in self.finish() }
    }

    nonisolated func audioPlayerDecodeErrorDidOccur(_ player: AVAudioPlayer, error: Error?) {
        Task { @MainActor in self.finish() }
    }
}

enum CarGreeting {
    /// What the brain sees; config/prompts/surfaces/car.md says how to answer.
    static let prompt = "[CarPlay connected]"

    /// Ask for the greeting and speak it. Returns the text (for Siri's dialog).
    @discardableResult
    static func run() async -> String {
        do {
            let reply = try await JarvisClient.shared.askText(prompt, speak: true, source: "car")
            await JarvisSpeaker.shared.play(base64: reply.audioB64)
            return reply.text
        } catch {
            return ""
        }
    }
}
