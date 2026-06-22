import SwiftUI

struct ContentView: View {
    @EnvironmentObject var glassesManager: GlassesManager
    @EnvironmentObject var chatViewModel: ChatViewModel

    var body: some View {
        TabView {
            ChatView()
                .tabItem {
                    Label("Chat", systemImage: "bubble.left.and.bubble.right.fill")
                }

            GlassesStatusView()
                .tabItem {
                    Label("Glasses", systemImage: "eyeglasses")
                }

            SettingsView()
                .tabItem {
                    Label("Settings", systemImage: "gearshape.fill")
                }
        }
    }
}

struct GlassesStatusView: View {
    @EnvironmentObject var glassesManager: GlassesManager

    var body: some View {
        NavigationStack {
            List {
                Section("Connection") {
                    LabeledContent("Session", value: glassesManager.sessionState.displayName)
                    LabeledContent("Display", value: glassesManager.displayState.displayName)
                    LabeledContent("Status") {
                        Circle()
                            .fill(glassesManager.isConnected ? Color.green : Color.red)
                            .frame(width: 10, height: 10)
                    }
                }

                if let error = glassesManager.errorMessage {
                    Section("Error") {
                        Text(error)
                            .foregroundStyle(.red)
                            .font(.caption)
                    }
                }

                Section {
                    Button("Reconnect") {
                        Task { await glassesManager.reconnect() }
                    }
                    .disabled(glassesManager.isConnected)

                    Button("Show Idle HUD") {
                        Task { await glassesManager.showIdleHUD() }
                    }
                    .disabled(!glassesManager.isConnected)
                }
            }
            .navigationTitle("Glasses")
        }
    }
}
