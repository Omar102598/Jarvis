#if DEBUG
import Foundation
import MWDATCamera
import MWDATCore
import MWDATMockDevice

/// Exercises the DAT pipeline against Mock Device Kit instead of real glasses.
///
/// Deliberately separate from `GlassesManager`: that manager's device selector
/// filters for `supportsDisplay()`, and the SDK resolved here (0.7.0 — pinned
/// in Package.resolved) can only mock a `MockRaybanMeta`, which conforms to
/// `MockDisplaylessGlasses`. It will never pass that filter, so routing it
/// through `GlassesManager` would just hang waiting for an eligible device
/// instead of proving anything. This runs its own unfiltered session instead.
///
/// Confirmed by reading the resolved SDK's binary interface directly, not the
/// docs: even the newest tagged release (0.8.0) has no display-capable model
/// in `GlassesModel` for Mock Device Kit. So this validates configuration,
/// registration, session lifecycle, and camera streaming — the pieces that
/// CAN be simulated — and does not attempt Display. HUDRenderer/GlassesManager's
/// Display path stays untestable until real Ray-Ban Display hardware arrives.
enum GlassesMockController {

    /// Opt-in only: add `--mock-glasses` to the scheme's launch arguments
    /// (Xcode > Edit Scheme > Run > Arguments) to activate. Off by default so
    /// ordinary debug runs still try to reach real glasses.
    static var isRequested: Bool {
        ProcessInfo.processInfo.arguments.contains("--mock-glasses")
    }

    @MainActor
    static func runSmokeTestIfRequested() {
        guard isRequested else { return }
        Task {
            do {
                try await runSmokeTest()
            } catch {
                print("[GlassesMockController] Smoke test FAILED: \(error)")
            }
        }
    }

    @MainActor
    private static func runSmokeTest() async throws {
        print("[GlassesMockController] Enabling Mock Device Kit…")
        // Enabled before configure(), on a hunch worth logging either way: if
        // MockDeviceKit needs to be armed before the SDK picks a transport,
        // arming it after a failed configure() wouldn't help. try? because
        // JarvisApp.init() already called configure() once; a fresh attempt
        // here after arming the mock should throw .alreadyConfigured if that
        // first call actually succeeded, which is fine to ignore.
        MockDeviceKit.shared.enable()
        try? Wearables.configure()

        let mock = MockDeviceKit.shared.pairRaybanMeta()
        mock.powerOn()
        mock.unfold()
        mock.don()
        print("[GlassesMockController] Paired + donned a mock Ray-Ban Meta.")

        let wearables = Wearables.shared
        try await wearables.startRegistration()

        // No supportsDisplay() filter here — see the type doc above.
        let selector = AutoDeviceSelector(wearables: wearables)
        let session = try wearables.createSession(deviceSelector: selector)
        try session.start()

        for await state in session.stateStream() {
            if state == .started { break }
        }
        print("[GlassesMockController] Session started against the mock device.")

        let config = StreamConfiguration(videoCodec: .raw, resolution: .low, frameRate: 15)
        guard let stream = try session.addStream(config: config) else {
            print("[GlassesMockController] Smoke test FAILED: addStream returned nil.")
            session.stop()
            return
        }
        await mock.services.camera.setCameraFeed(cameraFacing: .front)
        await stream.start()

        let captured: Bool = await withCheckedContinuation { continuation in
            var resumed = false
            let token = stream.photoDataPublisher.listen { photo in
                guard !resumed else { return }
                resumed = true
                continuation.resume(returning: !photo.data.isEmpty)
            }
            _ = token
            stream.capturePhoto(format: .jpeg)
        }
        print(captured
              ? "[GlassesMockController] Smoke test PASSED — registration, session, and camera capture all worked against the mock."
              : "[GlassesMockController] Smoke test FAILED — capturePhoto returned no data.")

        session.stop()
    }
}
#endif
