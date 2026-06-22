import AVFoundation
import Foundation

// Captures microphone audio until silence is detected, then returns PCM WAV data.
final class VoiceRecorder {
    private var audioEngine = AVAudioEngine()
    private var audioBuffer = Data()
    private var isCapturing = false
    private var silenceTimer: Timer?

    private let silenceThreshold: Float = 0.01       // RMS below this = silence
    private let silenceDurationSecs: Double = 1.5    // stop after this much silence
    private let maxDurationSecs: Double = 30.0        // hard stop

    // Returns WAV bytes or throws if recording fails.
    func record() async throws -> Data {
        try AVAudioSession.sharedInstance().setCategory(.record, mode: .measurement, options: [.duckOthers])
        try AVAudioSession.sharedInstance().setActive(true)

        return try await withCheckedThrowingContinuation { continuation in
            audioBuffer = Data()
            isCapturing = true

            let inputNode = audioEngine.inputNode
            let recordingFormat = inputNode.outputFormat(forBus: 0)

            inputNode.installTap(onBus: 0, bufferSize: 4096, format: recordingFormat) { [weak self] buffer, _ in
                guard let self, self.isCapturing else { return }
                self.appendPCM(buffer: buffer)

                let rms = self.calculateRMS(buffer: buffer)
                DispatchQueue.main.async {
                    if rms < self.silenceThreshold {
                        self.startSilenceTimer {
                            self.stop { result in
                                continuation.resume(with: result)
                            }
                        }
                    } else {
                        self.silenceTimer?.invalidate()
                        self.silenceTimer = nil
                    }
                }
            }

            do {
                try audioEngine.start()
            } catch {
                continuation.resume(throwing: error)
                return
            }

            // Hard stop after maxDurationSecs
            DispatchQueue.main.asyncAfter(deadline: .now() + maxDurationSecs) { [weak self] in
                self?.stop { result in
                    continuation.resume(with: result)
                }
            }
        }
    }

    func stopEarly() {
        stop(completion: nil)
    }

    private func stop(completion: ((Result<Data, Error>) -> Void)?) {
        guard isCapturing else { return }
        isCapturing = false
        silenceTimer?.invalidate()
        silenceTimer = nil
        audioEngine.inputNode.removeTap(onBus: 0)
        audioEngine.stop()
        try? AVAudioSession.sharedInstance().setActive(false, options: .notifyOthersOnDeactivation)
        let wav = buildWAV(from: audioBuffer, sampleRate: 16000, channels: 1)
        completion?(.success(wav))
    }

    private func startSilenceTimer(action: @escaping () -> Void) {
        guard silenceTimer == nil else { return }
        silenceTimer = Timer.scheduledTimer(withTimeInterval: silenceDurationSecs, repeats: false) { _ in
            action()
        }
    }

    // Downsample to 16 kHz mono (faster-whisper prefers this)
    private func appendPCM(buffer: AVAudioPCMBuffer) {
        guard let channelData = buffer.floatChannelData else { return }
        let frameCount = Int(buffer.frameLength)
        let samples = channelData[0]
        for i in 0..<frameCount {
            var sample = Int16(max(-32768, min(32767, samples[i] * 32767)))
            withUnsafeBytes(of: &sample) { audioBuffer.append(contentsOf: $0) }
        }
    }

    private func calculateRMS(buffer: AVAudioPCMBuffer) -> Float {
        guard let channelData = buffer.floatChannelData else { return 0 }
        let frameCount = Int(buffer.frameLength)
        var sum: Float = 0
        for i in 0..<frameCount { sum += channelData[0][i] * channelData[0][i] }
        return sqrt(sum / Float(frameCount))
    }

    // Build a minimal 16-bit mono WAV header around raw PCM data
    private func buildWAV(from pcmData: Data, sampleRate: UInt32, channels: UInt16) -> Data {
        let bitsPerSample: UInt16 = 16
        let byteRate = sampleRate * UInt32(channels) * UInt32(bitsPerSample) / 8
        let blockAlign = channels * bitsPerSample / 8
        let dataSize = UInt32(pcmData.count)
        let fileSize = 36 + dataSize

        var header = Data(count: 44)
        header.replaceSubrange(0..<4,   with: "RIFF".utf8)
        header.replaceSubrange(4..<8,   with: withUnsafeBytes(of: fileSize.littleEndian) { Data($0) })
        header.replaceSubrange(8..<12,  with: "WAVE".utf8)
        header.replaceSubrange(12..<16, with: "fmt ".utf8)
        header.replaceSubrange(16..<20, with: withUnsafeBytes(of: UInt32(16).littleEndian) { Data($0) })
        header.replaceSubrange(20..<22, with: withUnsafeBytes(of: UInt16(1).littleEndian) { Data($0) }) // PCM
        header.replaceSubrange(22..<24, with: withUnsafeBytes(of: channels.littleEndian) { Data($0) })
        header.replaceSubrange(24..<28, with: withUnsafeBytes(of: sampleRate.littleEndian) { Data($0) })
        header.replaceSubrange(28..<32, with: withUnsafeBytes(of: byteRate.littleEndian) { Data($0) })
        header.replaceSubrange(32..<34, with: withUnsafeBytes(of: blockAlign.littleEndian) { Data($0) })
        header.replaceSubrange(34..<36, with: withUnsafeBytes(of: bitsPerSample.littleEndian) { Data($0) })
        header.replaceSubrange(36..<40, with: "data".utf8)
        header.replaceSubrange(40..<44, with: withUnsafeBytes(of: dataSize.littleEndian) { Data($0) })

        return header + pcmData
    }
}
