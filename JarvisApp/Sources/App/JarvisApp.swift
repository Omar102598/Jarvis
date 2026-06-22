import SwiftUI
import MWDATCore

@main
struct JarvisApp: App {
    @StateObject private var glassesManager = GlassesManager()
    @StateObject private var chatViewModel = ChatViewModel()

    init() {
        do {
            try Wearables.configure()
        } catch {
            print("[JarvisApp] Wearables SDK configuration failed: \(error)")
        }
    }

    var body: some Scene {
        WindowGroup {
            ContentView()
                .environmentObject(glassesManager)
                .environmentObject(chatViewModel)
                .onOpenURL { url in
                    // Meta AI OAuth callback after registration/permission flow
                    Task { _ = try? await Wearables.shared.handleUrl(url) }
                }
                .task {
                    await glassesManager.start()
                    await chatViewModel.connectWebSocket()
                    PresenceManager.shared.startHeartbeat()
                }
        }
    }
}
