import SwiftUI
import MWDATCore

@main
struct JarvisApp: App {
    @StateObject private var glassesManager = GlassesManager()
    @StateObject private var chatViewModel = ChatViewModel()
    @Environment(\.scenePhase) private var scenePhase

    init() {
        // Match the window background to the app background before SwiftUI renders,
        // so the area behind the safe areas (top notch, bottom home indicator) is never black.
        UIWindow.appearance().backgroundColor = UIColor(red: 3/255, green: 8/255, blue: 16/255, alpha: 1)

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
                    await chatViewModel.loadHistory()
                    chatViewModel.requestNotificationPermission()
                    PresenceManager.shared.startHeartbeat()
                    LocationManager.shared.start()
                    // Month 4: push HealthKit + calendar context to the backend
                    await HealthKitManager.shared.syncOnLaunch()
                    await CalendarManager.shared.syncOnLaunch()
                }
                .onChange(of: scenePhase) { _, phase in
                    // Re-sync fitness + calendar context each time the app returns
                    // to the foreground so the ambient agent always has fresh data,
                    // and pull chat turns that happened on other surfaces meanwhile.
                    guard phase == .active else { return }
                    Task {
                        await chatViewModel.refreshHistory()
                        await HealthKitManager.shared.pushSnapshot()
                        await CalendarManager.shared.pushNextEvent()
                    }
                }
        }
    }
}
