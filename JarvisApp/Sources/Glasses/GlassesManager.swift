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
        #if DEBUG
        // With --mock-glasses the only device is a displayless MockRaybanMeta,
        // which can never satisfy this manager's supportsDisplay() filter. Before
        // the selector wait that surfaced as a confusing "Start error:
        // noEligibleDevice" next to the smoke test's own output; now it would
        // block on activeDeviceStream indefinitely, since the selector will
        // never choose a device it has filtered out. Stand down and let the
        // smoke test own the SDK for that run.
        if GlassesMockController.isRequested {
            print("[GlassesManager] --mock-glasses set — standing down "
                  + "(a displayless mock cannot satisfy supportsDisplay()).")
            return
        }
        #endif
        do {
            do {
                try await wearables.startRegistration()
            } catch RegistrationError.alreadyRegistered {
                // Registration persists across launches, so every launch after
                // the first one throws here. Treating that as a failure aborted
                // start() before it ever reached the session — the glasses would
                // connect exactly once, on the install that registered them, and
                // silently never again. Already registered is the good case.
            }
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
        // Wait until the selector has actually chosen a device. devicesStream
        // (what the SDK can see) is not the same as activeDeviceStream (what
        // this selector picked) — and only the latter means createSession will
        // succeed. On real glasses this is the difference between connecting
        // and failing when the app opens just before Bluetooth settles.
        for await device in selector.activeDeviceStream() where device != nil { break }

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

    /// Token for the in-flight photo listener.
    ///
    /// Held as a property deliberately. `_ = token` inside the continuation did
    /// NOT keep it alive — discarding to `_` releases immediately, the SDK drops
    /// the listener, the callback never fires, and the continuation is left
    /// suspended forever. The old comment claimed the opposite of what the line
    /// did, and the symptom was a camera button that simply never returned.
    private var photoToken: (any AnyListenerToken)?

    func capturePhoto() async throws -> Data {
        guard let stream = cameraStream else { throw GlassesError.notConnected }

        return try await withCheckedThrowingContinuation { continuation in
            let box = ResumeOnce(continuation)
            photoToken = stream.photoDataPublisher.listen { photo in
                box.resume(returning: photo.data)
            }
            stream.capturePhoto(format: .jpeg)

            // A capture that never calls back must fail rather than hang. The
            // glasses can be doffed, folded, or out of range between the request
            // and the response, and none of those produce a photo event.
            Task { [weak self] in
                try? await Task.sleep(nanoseconds: 15_000_000_000)
                if box.resume(throwing: GlassesError.captureTimedOut) {
                    await MainActor.run { self?.photoToken = nil }
                }
            }
        }
    }
}

// MARK: - Errors

enum GlassesError: LocalizedError {
    case notConnected
    case captureTimedOut

    var errorDescription: String? {
        switch self {
        case .notConnected: return "Glasses not connected or camera stream unavailable."
        case .captureTimedOut: return "The glasses didn't return a photo in time."
        }
    }
}

/// Resumes a continuation exactly once, from any thread.
///
/// Both the photo callback and the timeout can fire, and resuming a checked
/// continuation twice is a crash rather than a warning. The previous `var
/// resumed = false` captured in a closure was not safe across threads.
private final class ResumeOnce<T>: @unchecked Sendable {
    private let lock = NSLock()
    private var continuation: CheckedContinuation<T, Error>?

    init(_ continuation: CheckedContinuation<T, Error>) {
        self.continuation = continuation
    }

    /// Returns true if this call is the one that resumed it.
    @discardableResult
    func resume(returning value: T) -> Bool {
        lock.lock(); defer { lock.unlock() }
        guard let c = continuation else { return false }
        continuation = nil
        c.resume(returning: value)
        return true
    }

    @discardableResult
    func resume(throwing error: Error) -> Bool {
        lock.lock(); defer { lock.unlock() }
        guard let c = continuation else { return false }
        continuation = nil
        c.resume(throwing: error)
        return true
    }
}

// MARK: - Wake notification

extension Notification.Name {
    static let jarvisActivateWake = Notification.Name("jarvisActivateWake")
}
