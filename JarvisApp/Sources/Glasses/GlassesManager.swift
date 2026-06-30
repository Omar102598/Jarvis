import Foundation
import MWDATCore
import MWDATCamera
import MWDATDisplay

// MARK: - State display helpers

extension DeviceSessionState {
    var displayName: String {
        switch self {
        case .stopped: return "Stopped"
        case .starting: return "Connecting..."
        case .started: return "Connected"
        case .stopping: return "Disconnecting"
        @unknown default: return "Unknown"
        }
    }
}

extension DisplayState {
    var displayName: String {
        switch self {
        case .stopped: return "Off"
        case .starting: return "Starting..."
        case .started: return "Active"
        case .stopping: return "Stopping"
        @unknown default: return "Unknown"
        }
    }
}

// MARK: - GlassesManager

@MainActor
final class GlassesManager: ObservableObject {
    @Published var sessionState: DeviceSessionState = .stopped
    @Published var displayState: DisplayState = .stopped
    @Published var isConnected: Bool = false
    @Published var errorMessage: String?

    private var session: DeviceSession?
    private var display: Display?
    private var cameraStream: MWDATCamera.Stream?
    private var displayListenerToken: (any AnyListenerToken)?
    private var stateTask: Task<Void, Never>?

    private let wearables = Wearables.shared
    let renderer = HUDRenderer()

    // MARK: Lifecycle

    func start() async {
        do {
            try await wearables.startRegistration()
            let status = try await wearables.requestPermission(.camera)
            guard status == .granted else {
                errorMessage = "Camera permission denied by user."
                return
            }
            try await connectSession()
        } catch {
            errorMessage = error.localizedDescription
            print("[GlassesManager] Start error: \(error)")
        }
    }

    func reconnect() async {
        disconnect()
        errorMessage = nil
        await start()
    }

    func disconnect() {
        stateTask?.cancel()
        stateTask = nil
        session?.stop()
        session = nil
        display = nil
        cameraStream = nil
        displayListenerToken = nil
        isConnected = false
        sessionState = .stopped
        displayState = .stopped
    }

    // MARK: Session

    private func connectSession() async throws {
        let selector = AutoDeviceSelector(
            wearables: wearables,
            filter: { $0.supportsDisplay() }
        )
        let newSession = try wearables.createSession(deviceSelector: selector)
        self.session = newSession

        stateTask = Task { [weak self] in
            guard let self else { return }
            for await state in newSession.stateStream() {
                await MainActor.run {
                    self.sessionState = state
                    self.isConnected = (state == .started)
                }
                if state == .started {
                    do {
                        try await self.attachCapabilities(to: newSession)
                    } catch {
                        await MainActor.run {
                            self.errorMessage = "Capability attach failed: \(error.localizedDescription)"
                        }
                    }
                }
            }
        }

        try newSession.start()
    }

    private func attachCapabilities(to session: DeviceSession) async throws {
        // Add display capability
        let newDisplay = try session.addDisplay()
        self.display = newDisplay

        self.displayListenerToken = newDisplay.statePublisher.listen { [weak self] state in
            Task { @MainActor [weak self] in
                guard let self else { return }
                self.displayState = state
                if state == .started {
                    try? await self.showIdleHUD()
                }
            }
        }

        await newDisplay.start()

        // Add camera stream for on-demand photo captures
        let config = StreamConfiguration(videoCodec: .raw, resolution: .low, frameRate: 15)
        if let stream = try session.addStream(config: config) {
            self.cameraStream = stream
            await stream.start()
        }
    }

    // MARK: HUD control

    func showIdleHUD() async throws {
        guard let display, displayState == .started else { return }
        try await renderer.send(.idle, to: display)
    }

    func send(_ state: HUDState) async {
        guard let display, displayState == .started else { return }
        try? await renderer.send(state, to: display)
    }

    // MARK: Camera

    func capturePhoto() async throws -> Data {
        guard let stream = cameraStream else { throw GlassesError.notConnected }

        return try await withCheckedThrowingContinuation { continuation in
            var resumed = false
            let token = stream.photoDataPublisher.listen { photo in
                guard !resumed else { return }
                resumed = true
                continuation.resume(returning: photo.data)
            }
            _ = token  // keep token alive until callback fires
            stream.capturePhoto(format: .jpeg)
        }
    }
}

// MARK: - Errors

enum GlassesError: LocalizedError {
    case notConnected

    var errorDescription: String? {
        switch self {
        case .notConnected: return "Glasses not connected or camera stream unavailable."
        }
    }
}

// MARK: - Wake notification

extension Notification.Name {
    static let jarvisActivateWake = Notification.Name("jarvisActivateWake")
}
