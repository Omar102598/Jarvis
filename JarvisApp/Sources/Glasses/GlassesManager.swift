import Foundation
import Combine
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
    private var cameraStream: Stream?
    private var cancellables = Set<AnyCancellable>()
    private var stateTask: Task<Void, Never>?

    private let wearables = Wearables.shared
    let renderer = HUDRenderer()

    // MARK: Lifecycle

    func start() async {
        do {
            try await wearables.startRegistration()
            let status = try await wearables.requestPermission(.camera)
            guard status == .authorized else {
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
        cancellables.removeAll()
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

        newDisplay.statePublisher
            .receive(on: DispatchQueue.main)
            .sink { [weak self] state in
                guard let self else { return }
                self.displayState = state
                if state == .started {
                    Task { @MainActor in try? await self.showIdleHUD() }
                }
            }
            .store(in: &cancellables)

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
            var captureToken: AnyCancellable?
            captureToken = stream.photoDataPublisher
                .first()
                .sink(
                    receiveCompletion: { completion in
                        if case .failure(let error) = completion {
                            continuation.resume(throwing: error)
                        }
                        captureToken?.cancel()
                    },
                    receiveValue: { photo in
                        continuation.resume(returning: photo.data)
                        captureToken?.cancel()
                    }
                )
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
