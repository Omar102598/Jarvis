import SwiftUI
import JarvisShared

@main
struct JarvisDesktopApp: App {
    @StateObject private var serviceManager    = ServiceManager()
    @StateObject private var chatViewModel     = DesktopChatViewModel()
    @StateObject private var notificationRouter = NotificationRouter()

    var body: some Scene {
        MenuBarExtra {
            StatusBarMenuView()
                .environmentObject(serviceManager)
                .environmentObject(chatViewModel)
        } label: {
            // Label is always visible — use .task here to auto-start at launch
            Image(systemName: "brain.head.profile")
                .task {
                    await serviceManager.startAll()
                    notificationRouter.start()
                }
        }
        .menuBarExtraStyle(.menu)

        Window("Jarvis Chat", id: "chat") {
            DesktopChatView()
                .environmentObject(chatViewModel)
                .environmentObject(serviceManager)
                .frame(minWidth: 380, maxWidth: 600, minHeight: 500)
        }
        .defaultSize(width: 420, height: 620)

        Window("Jarvis Dashboard", id: "dashboard") {
            DashboardView()
                .frame(minWidth: 900, minHeight: 600)
        }
        .defaultSize(width: 1200, height: 800)

        Settings {
            DesktopSettingsView()
                .environmentObject(serviceManager)
                .frame(width: 480)
        }
    }
}
