import SwiftUI

struct StatusBarMenuView: View {
    @EnvironmentObject var serviceManager: ServiceManager
    @EnvironmentObject var chatViewModel: DesktopChatViewModel
    @Environment(\.openWindow) private var openWindow

    var body: some View {
        // Status row
        HStack(spacing: 6) {
            Circle()
                .fill(serviceManager.statusColor)
                .frame(width: 8, height: 8)
            Text(serviceManager.statusLabel)
                .font(.system(size: 13, weight: .medium))
        }
        .padding(.vertical, 2)

        Divider()

        Button("Open Chat") {
            openWindow(id: "chat")
            NSApp.activate(ignoringOtherApps: true)
        }
        .keyboardShortcut("j", modifiers: [.command, .option])

        Button("Open Dashboard") {
            openWindow(id: "dashboard")
            NSApp.activate(ignoringOtherApps: true)
        }

        Divider()

        Menu("Services") {
            serviceStatusRow("mac_bridge", state: serviceManager.macBridgeState)
            serviceStatusRow("Docker Services", state: serviceManager.dockerState)
            serviceStatusRow("Audio Pipeline", state: serviceManager.audioState)

            Divider()

            Button("Start All") {
                Task { await serviceManager.startAll() }
            }
            Button("Stop All") {
                Task { await serviceManager.stopAll() }
            }
        }

        Divider()

        SettingsLink()
            .keyboardShortcut(",", modifiers: .command)

        Divider()

        Button("Quit Jarvis") {
            NSApp.terminate(nil)
        }
        .keyboardShortcut("q", modifiers: .command)
    }

    @ViewBuilder
    private func serviceStatusRow(_ name: String, state: ServiceManager.ServiceState) -> some View {
        Label {
            Text("\(name): \(state.label)")
        } icon: {
            Circle()
                .fill(state.color)
                .frame(width: 6, height: 6)
        }
        .foregroundColor(.secondary)
    }
}
