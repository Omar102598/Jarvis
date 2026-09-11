#if DEBUG
import Foundation
import UIKit
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
/// Resumes a non-throwing continuation exactly once, from any thread — the
/// photo callback and the timeout race each other, and a double resume is a
/// crash rather than a warning.
private final class MockResumeOnce: @unchecked Sendable {
    private let lock = NSLock()
    private var continuation: CheckedContinuation<Bool, Never>?

    init(_ continuation: CheckedContinuation<Bool, Never>) {
        self.continuation = continuation
    }

    func resume(_ value: Bool) {
        lock.lock(); defer { lock.unlock() }
        guard let c = continuation else { return }
        continuation = nil
        c.resume(returning: value)
    }
}

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

    /// A small JPEG on disk for the mock camera to "capture".
    ///
    /// Generated rather than bundled so there is no asset to keep in sync, and
    /// written to the temp directory so it cleans itself up.
    private static func makeFixtureImage() -> URL? {
        let size = CGSize(width: 64, height: 64)
        let image = UIGraphicsImageRenderer(size: size).image { ctx in
            UIColor.systemTeal.setFill()
            ctx.fill(CGRect(origin: .zero, size: size))
        }
        guard let data = image.jpegData(compressionQuality: 0.8) else { return nil }
        let url = FileManager.default.temporaryDirectory
            .appendingPathComponent("jarvis-mock-capture.jpg")
        do {
            try data.write(to: url)
            return url
        } catch {
            print("[GlassesMockController] fixture write failed: \(error)")
            return nil
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
        do {
            try await wearables.startRegistration()
        } catch RegistrationError.alreadyRegistered {
            // MockDeviceKitConfig defaults to initiallyRegistered: true, so the
            // mock is registered the moment it is enabled and this always throws.
            print("[GlassesMockController] Already registered (mock) — continuing.")
        }

        // No supportsDisplay() filter here — see the type doc above.
        let selector = AutoDeviceSelector(wearables: wearables)

        // Wait on the SELECTOR's activeDeviceStream, not wearables.devicesStream.
        // devicesStream reports what the SDK can SEE; activeDeviceStream reports
        // what this selector has actually CHOSEN. A device shows up in the first
        // while the second is still nil, which is why an earlier attempt logged
        // "Device visible to the SDK (1)" and then still threw noEligibleDevice.
        var selected = false
        for await device in selector.activeDeviceStream() where device != nil {
            selected = true
            print("[GlassesMockController] Selector picked a device.")
            break
        }
        guard selected else {
            print("[GlassesMockController] Smoke test FAILED: selector never chose a device.")
            return
        }

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
        // Feed a generated image, NOT the phone's camera.
        //
        // setCameraFeed(cameraFacing:) drives the real camera, which needs
        // camera permission and a working capture session. Reinstalling revokes
        // that permission, and this runs at app init before any UI could prompt
        // for it — which is what produced "FigCaptureSourceRemote err=-17281"
        // and a capture that never returned an image. A smoke test should not
        // depend on hardware or a permission dialog anyway: the point is to
        // prove the DAT pipeline carries bytes end to end, and a known file
        // does that deterministically.
        guard let fixture = Self.makeFixtureImage() else {
            print("[GlassesMockController] Smoke test FAILED: could not write the fixture image.")
            session.stop()
            return
        }
        mock.services.camera.setCapturedImage(fileURL: fixture)
        await stream.start()

        // Same trap as GlassesManager had: `_ = token` releases the listener
        // immediately, so the callback never fires and the continuation leaks —
        // which is exactly what "runSmokeTest() leaked its continuation" was
        // reporting. Hold it in a local that outlives the continuation, and time
        // out rather than hanging the test forever.
        var photoToken: (any AnyListenerToken)?
        let captured: Bool = await withCheckedContinuation { continuation in
            let state = MockResumeOnce(continuation)
            photoToken = stream.photoDataPublisher.listen { photo in
                state.resume(!photo.data.isEmpty)
            }
            stream.capturePhoto(format: .jpeg)
            Task {
                try? await Task.sleep(nanoseconds: 15_000_000_000)
                state.resume(false)
            }
        }
        photoToken = nil
        print(captured
              ? "[GlassesMockController] Smoke test PASSED — registration, session, and camera capture all worked against the mock."
              : "[GlassesMockController] Smoke test FAILED — capturePhoto returned no data.")

        session.stop()
    }
}
#endif
