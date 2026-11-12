import Foundation
import AVFoundation
import UIKit

/// Thumbnails of the photos and videos the user sent, kept on the phone.
///
/// The server never keeps them: the gateway parks the upload in Redis for ten
/// minutes, and the conversation history records only "[camera image]". So
/// without a local copy, the moment the chat is rebuilt from /history (every
/// foreground) the user's photo is gone from their own message. Keyed by turn
/// id, which history carries on the user's entry too.
@MainActor
enum ChatMediaCache {
    private static var dir: URL? {
        guard let base = FileManager.default.urls(for: .cachesDirectory,
                                                  in: .userDomainMask).first
        else { return nil }
        let d = base.appendingPathComponent("chat-media", isDirectory: true)
        try? FileManager.default.createDirectory(at: d, withIntermediateDirectories: true)
        return d
    }

    private static func file(_ turnID: String) -> URL? {
        let safe = turnID.filter { $0.isLetter || $0.isNumber || $0 == "-" }
        guard !safe.isEmpty else { return nil }
        return dir?.appendingPathComponent("\(safe).jpg")
    }

    static func save(_ data: Data, turnID: String) {
        guard let url = file(turnID) else { return }
        try? data.write(to: url, options: .atomic)
    }

    static func load(turnID: String) -> Data? {
        guard let url = file(turnID) else { return nil }
        return try? Data(contentsOf: url)
    }

    /// A small JPEG for the chat bubble: big enough to recognise, small enough
    /// that a long chat history costs little memory or disk.
    static func thumbnail(fromImage data: Data) -> Data? {
        let jpeg = ChatViewModel.downscaledJPEG(data, maxEdge: 720, quality: 0.7)
        return UIImage(data: jpeg) == nil ? nil : jpeg
    }

    /// First frame of a video, as a thumbnail.
    static func thumbnail(fromVideo data: Data) -> Data? {
        let tmp = FileManager.default.temporaryDirectory
            .appendingPathComponent("thumb-\(UUID().uuidString).mov")
        defer { try? FileManager.default.removeItem(at: tmp) }
        guard (try? data.write(to: tmp)) != nil else { return nil }
        let gen = AVAssetImageGenerator(asset: AVURLAsset(url: tmp))
        gen.appliesPreferredTrackTransform = true
        gen.maximumSize = CGSize(width: 720, height: 720)
        guard let cg = try? gen.copyCGImage(at: .zero, actualTime: nil) else { return nil }
        return UIImage(cgImage: cg).jpegData(compressionQuality: 0.7)
    }

    /// History stores an attached image as a "[camera image]" line ahead of the
    /// prompt (one per frame for a video). Strip it from the visible text.
    static func stripMarkers(_ text: String) -> (text: String, hadMedia: Bool) {
        guard text.contains("[camera image]") else { return (text, false) }
        let cleaned = text.replacingOccurrences(of: "[camera image]", with: "")
            .trimmingCharacters(in: .whitespacesAndNewlines)
        return (cleaned, true)
    }
}
