import SwiftUI
import UIKit
import UniformTypeIdentifiers

/// Live capture — photo *or* video — via `UIImagePickerController`.
///
/// SwiftUI's `PhotosPicker` only reads the existing library, which is why the
/// camera button had no live path on the phone: with glasses connected it shot
/// through the glasses, and without them it fell straight to the library. This
/// fills that gap.
///
/// Video is capped at 30s at medium quality on purpose. `/ask/video` uploads
/// the clip base64-inline and the gateway rejects anything over 40MB, so an
/// unbounded 4K recording would upload slowly and *then* fail server-side —
/// the worst possible place to discover the limit.
struct CameraPicker: UIViewControllerRepresentable {

    enum Capture {
        case image(Data)
        case video(Data)
    }

    var onCapture: (Capture) -> Void

    @Environment(\.dismiss) private var dismiss

    /// False on the Simulator and on any device without a usable camera —
    /// presenting the picker there shows a black screen with no way back.
    static var isAvailable: Bool {
        UIImagePickerController.isSourceTypeAvailable(.camera)
    }

    func makeUIViewController(context: Context) -> UIImagePickerController {
        let picker = UIImagePickerController()
        picker.sourceType = .camera
        picker.mediaTypes = [UTType.image.identifier, UTType.movie.identifier]
        picker.videoQuality = .typeMedium
        picker.videoMaximumDuration = 30
        picker.delegate = context.coordinator
        return picker
    }

    func updateUIViewController(_ picker: UIImagePickerController, context: Context) {}

    func makeCoordinator() -> Coordinator { Coordinator(self) }

    final class Coordinator: NSObject, UIImagePickerControllerDelegate,
                             UINavigationControllerDelegate {
        private let parent: CameraPicker

        init(_ parent: CameraPicker) { self.parent = parent }

        func imagePickerController(
            _ picker: UIImagePickerController,
            didFinishPickingMediaWithInfo info: [UIImagePickerController.InfoKey: Any]
        ) {
            defer { parent.dismiss() }

            // A recorded clip arrives as a temp file URL; a photo arrives as a
            // UIImage. Check the movie case first — .originalImage is nil for
            // video, but being explicit keeps the intent obvious.
            if let url = info[.mediaURL] as? URL,
               let data = try? Data(contentsOf: url) {
                parent.onCapture(.video(data))
            } else if let image = info[.originalImage] as? UIImage,
                      let data = image.jpegData(compressionQuality: 0.8) {
                parent.onCapture(.image(data))
            }
        }

        func imagePickerControllerDidCancel(_ picker: UIImagePickerController) {
            parent.dismiss()
        }
    }
}
