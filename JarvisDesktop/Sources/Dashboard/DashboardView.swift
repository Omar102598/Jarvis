import SwiftUI
import WebKit

/// Embeds the Jarvis web dashboard (FastAPI at localhost:8888) in a WKWebView.
/// Exposes a "jarvisDesktop" JS message handler so future dashboard widgets
/// can call window.webkit.messageHandlers.jarvisDesktop.postMessage({...})
/// to trigger native Mac actions.
struct DashboardView: NSViewRepresentable {
    func makeCoordinator() -> Coordinator { Coordinator() }

    func makeNSView(context: Context) -> WKWebView {
        let userContent = WKUserContentController()
        userContent.add(context.coordinator, name: "jarvisDesktop")

        let config = WKWebViewConfiguration()
        config.userContentController = userContent

        let webView = WKWebView(frame: .zero, configuration: config)
        webView.load(URLRequest(url: URL(string: "http://localhost:8888")!))
        return webView
    }

    func updateNSView(_ nsView: WKWebView, context: Context) {}

    final class Coordinator: NSObject, WKScriptMessageHandler {
        func userContentController(
            _ controller: WKUserContentController,
            didReceive message: WKScriptMessage
        ) {
            guard let body = message.body as? [String: Any] else { return }
            let action = body["action"] as? String ?? ""
            print("[DashboardView] JS → native: action=\(action), payload=\(body)")
            // Future: dispatch native actions (open file, run script, etc.)
        }
    }
}
