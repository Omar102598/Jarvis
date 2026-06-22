import SwiftUI
import JarvisShared

@MainActor
final class ServiceManager: ObservableObject {

    enum ServiceState {
        case unknown, stopped, starting, running, error(String)

        var label: String {
            switch self {
            case .unknown:          return "unknown"
            case .stopped:          return "stopped"
            case .starting:         return "starting…"
            case .running:          return "running"
            case .error(let msg):   return "error: \(msg)"
            }
        }

        var color: Color {
            switch self {
            case .running:  return .green
            case .error:    return .red
            case .starting: return .yellow
            default:        return .gray
            }
        }

        var icon: String {
            switch self {
            case .running:  return "checkmark.circle.fill"
            case .error:    return "xmark.circle.fill"
            case .starting: return "arrow.clockwise.circle"
            default:        return "circle"
            }
        }
    }

    @Published var macBridgeState: ServiceState = .unknown
    @Published var dockerState: ServiceState = .unknown
    @Published var audioState: ServiceState = .unknown

    var statusColor: Color {
        switch macBridgeState {
        case .running:  return .green
        case .error:    return .red
        case .starting: return .yellow
        default:        return .gray
        }
    }

    var statusLabel: String {
        switch macBridgeState {
        case .running:  return "Jarvis Ready"
        case .starting: return "Starting…"
        case .stopped:  return "Jarvis Stopped"
        case .error:    return "Service Error"
        default:        return "Jarvis"
        }
    }

    // MARK: - Repo path detection

    private var repoPath: String {
        let saved = JarvisConfig.repoPath
        if !saved.isEmpty { return saved }
        // Walk up from bundle executable to find docker-compose.yml
        var url = Bundle.main.bundleURL
        for _ in 0..<8 {
            url = url.deletingLastPathComponent()
            if FileManager.default.fileExists(atPath: url.appendingPathComponent("docker-compose.yml").path) {
                return url.path
            }
        }
        return NSHomeDirectory() + "/Documents/GitHub/Jarvis"
    }

    // MARK: - Public API

    func startAll() async {
        await startMacBridge()
        async let docker: Void = startDockerServices()
        async let audio: Void = startAudioPipeline()
        _ = await (docker, audio)
        startHealthPolling()
        let hostname = Host.current().localizedName ?? "desktop"
        PresenceManager.shared.startHeartbeat(
            surfaceId: "mac-\(hostname)",
            type: "mac",
            capabilities: ["display", "audio", "notifications"]
        )
    }

    func stopAll() async {
        PresenceManager.shared.stopHeartbeat()
        await withTaskGroup(of: Void.self) { group in
            group.addTask { await self.stopDockerServices() }
            group.addTask { await self.stopMacBridge() }
            group.addTask { await self.stopAudioPipeline() }
        }
    }

    // MARK: - mac_bridge

    private func startMacBridge() async {
        macBridgeState = .starting
        _ = await runScript("bash \(repoPath)/scripts/run_mac_bridge.sh start")
        for _ in 0..<30 {
            if await checkHealth("http://localhost:7777/health") {
                macBridgeState = .running
                return
            }
            try? await Task.sleep(nanoseconds: 1_000_000_000)
        }
        macBridgeState = .error("health check timeout")
    }

    private func stopMacBridge() async {
        _ = await runScript("bash \(repoPath)/scripts/run_mac_bridge.sh stop")
        macBridgeState = .stopped
    }

    // MARK: - Docker services

    private func startDockerServices() async {
        dockerState = .starting
        let out = await runScript("docker compose -f \(repoPath)/docker-compose.yml up -d 2>&1")
        dockerState = out.lowercased().contains("error") ? .error("see logs") : .running
    }

    private func stopDockerServices() async {
        _ = await runScript("docker compose -f \(repoPath)/docker-compose.yml down 2>&1")
        dockerState = .stopped
    }

    // MARK: - Audio pipeline

    private func startAudioPipeline() async {
        audioState = .starting
        let out = await runScript("bash \(repoPath)/scripts/run_audio_native.sh start 2>&1")
        audioState = out.isEmpty ? .error("start failed") : .running
    }

    private func stopAudioPipeline() async {
        _ = await runScript("bash \(repoPath)/scripts/run_audio_native.sh stop 2>&1")
        audioState = .stopped
    }

    // MARK: - Health polling

    private var healthTask: Task<Void, Never>?

    private func startHealthPolling() {
        healthTask?.cancel()
        healthTask = Task { [weak self] in
            while !Task.isCancelled {
                guard let self else { return }
                let bridgeOk = await self.checkHealth("http://localhost:7777/health")
                let dashOk   = await self.checkHealth("http://localhost:8888/health")
                await MainActor.run {
                    self.macBridgeState = bridgeOk ? .running : .error("unreachable")
                    self.dockerState    = dashOk   ? .running : .error("unreachable")
                }
                try? await Task.sleep(nanoseconds: 30_000_000_000)
            }
        }
    }

    // MARK: - Helpers

    private func checkHealth(_ url: String) async -> Bool {
        guard let reqURL = URL(string: url) else { return false }
        do {
            let (_, response) = try await URLSession.shared.data(from: reqURL)
            return (response as? HTTPURLResponse)?.statusCode == 200
        } catch {
            return false
        }
    }

    private func runScript(_ cmd: String) async -> String {
        await withCheckedContinuation { continuation in
            DispatchQueue.global(qos: .utility).async {
                let process = Process()
                process.executableURL = URL(fileURLWithPath: "/bin/bash")
                process.arguments = ["-c", cmd]
                let pipe = Pipe()
                process.standardOutput = pipe
                process.standardError  = pipe
                try? process.run()
                process.waitUntilExit()
                let output = String(data: pipe.fileHandleForReading.readDataToEndOfFile(), encoding: .utf8) ?? ""
                continuation.resume(returning: output)
            }
        }
    }
}
