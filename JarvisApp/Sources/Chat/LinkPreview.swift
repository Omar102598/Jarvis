import SwiftUI
import LinkPresentation
import UIKit

// MARK: - Link previews
//
// Links in a Jarvis reply render as cards with the page's image and title, the
// way iMessage shows them. Apple's LinkPresentation fetches the page's metadata
// (og:image, title) on the phone, so this works for any site, with no server work.

/// What a fetched page gives us. Cached for the app's lifetime, because chat
/// views are rebuilt constantly and a refetch would make the card flicker.
struct LinkPreviewData {
    let title: String
    let host: String
    let image: UIImage?
}

@MainActor
final class LinkPreviewCache {
    static let shared = LinkPreviewCache()
    private var done: [URL: LinkPreviewData] = [:]
    private var inFlight: [URL: Task<LinkPreviewData?, Never>] = [:]

    func preview(for url: URL) async -> LinkPreviewData? {
        if let d = done[url] { return d }
        if let t = inFlight[url] { return await t.value }
        let task = Task<LinkPreviewData?, Never> {
            let provider = LPMetadataProvider()
            provider.timeout = 12
            guard let meta = try? await provider.startFetchingMetadata(for: url) else { return nil }
            let image = await Self.loadImage(meta.imageProvider ?? meta.iconProvider)
            return LinkPreviewData(title: meta.title ?? "",
                                   host: Self.host(of: url),
                                   image: image)
        }
        inFlight[url] = task
        let result = await task.value
        inFlight[url] = nil
        if let result { done[url] = result }
        return result
    }

    static func host(of url: URL) -> String {
        let h = url.host ?? url.absoluteString
        return h.hasPrefix("www.") ? String(h.dropFirst(4)) : h
    }

    private static func loadImage(_ provider: NSItemProvider?) async -> UIImage? {
        guard let provider, provider.canLoadObject(ofClass: UIImage.self) else { return nil }
        return await withCheckedContinuation { cont in
            provider.loadObject(ofClass: UIImage.self) { obj, _ in
                cont.resume(returning: obj as? UIImage)
            }
        }
    }

    /// Web links in `text`, in order, without duplicates. Media files are left
    /// to MediaCard, and `excluding` drops links a product card already shows.
    static func links(in text: String, excluding: Set<String> = [], limit: Int = 3) -> [URL] {
        guard let detector = try? NSDataDetector(types: NSTextCheckingResult.CheckingType.link.rawValue)
        else { return [] }
        let range = NSRange(text.startIndex..., in: text)
        var seen = Set<String>(), out: [URL] = []
        for match in detector.matches(in: text, range: range) {
            guard let url = match.url, let scheme = url.scheme?.lowercased(),
                  scheme == "https" || scheme == "http" else { continue }
            let key = url.absoluteString
            let ext = url.pathExtension.lowercased()
            if ["jpg", "jpeg", "png", "gif", "webp", "mp4", "mov", "m3u8"].contains(ext) { continue }
            if excluding.contains(key) || !seen.insert(key).inserted { continue }
            out.append(url)
            if out.count == limit { break }
        }
        return out
    }
}

/// The previews for every link in a reply.
struct LinkPreviewList: View {
    let urls: [URL]

    var body: some View {
        VStack(alignment: .leading, spacing: 8) {
            ForEach(urls, id: \.absoluteString) { LinkPreviewCard(url: $0) }
        }
    }
}

struct LinkPreviewCard: View {
    let url: URL
    @State private var data: LinkPreviewData?
    @State private var loaded = false
    @Environment(\.openURL) private var openURL

    var body: some View {
        Button { openURL(url) } label: {
            VStack(alignment: .leading, spacing: 0) {
                if let img = data?.image, img.size.width >= 200 {
                    // A real preview image (product photo, article hero).
                    Image(uiImage: img)
                        .resizable()
                        .scaledToFill()
                        .frame(maxWidth: .infinity)
                        .frame(height: 150)
                        .background(Color.white)
                        .clipped()
                }
                HStack(spacing: 10) {
                    if let img = data?.image, img.size.width < 200 {
                        Image(uiImage: img).resizable().scaledToFit()
                            .frame(width: 28, height: 28).cornerRadius(6)
                    } else if data?.image == nil {
                        Image(systemName: loaded ? "link" : "globe")
                            .foregroundColor(.jBlue)
                            .frame(width: 28, height: 28)
                    }
                    VStack(alignment: .leading, spacing: 2) {
                        Text(title)
                            .font(.system(size: 12, weight: .semibold))
                            .foregroundColor(.jText)
                            .lineLimit(2)
                            .multilineTextAlignment(.leading)
                        Text(LinkPreviewCache.host(of: url))
                            .font(.system(size: 10))
                            .foregroundColor(.jText.opacity(0.5))
                    }
                    Spacer(minLength: 0)
                    if !loaded { ProgressView().scaleEffect(0.6).tint(.jBlue) }
                }
                .padding(10)
            }
            .frame(maxWidth: .infinity, alignment: .leading)
            .background(Color.jBg.opacity(0.7))
            .overlay(RoundedRectangle(cornerRadius: 10).stroke(Color.jBorder, lineWidth: 1))
            .cornerRadius(10)
        }
        .buttonStyle(.plain)
        .task(id: url) {
            data = await LinkPreviewCache.shared.preview(for: url)
            loaded = true
        }
    }

    private var title: String {
        if let t = data?.title, !t.isEmpty { return t }
        return loaded ? url.absoluteString : "Loading preview…"
    }
}
