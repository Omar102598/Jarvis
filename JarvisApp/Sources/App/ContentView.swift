import SwiftUI

struct ContentView: View {
    @EnvironmentObject var glassesManager: GlassesManager
    @EnvironmentObject var chatViewModel: ChatViewModel
    @AppStorage("devMode") private var devMode = false

    var body: some View {
        ZStack {
            // Fills the entire screen — including top notch and bottom home-indicator safe areas —
            // so the system window background (black) never bleeds through.
            Color.jBg.ignoresSafeArea()

            TabView {
                ChatView()
                    .tabItem {
                        Label("Chat", systemImage: "bubble.left.and.bubble.right")
                    }

                AgentsView()
                    .tabItem {
                        Label("Agents", systemImage: "person.3")
                    }

                if devMode {
                    ActivityView()
                        .tabItem {
                            Label("Activity", systemImage: "waveform.path.ecg")
                        }
                }

                GlassesStatusView()
                    .tabItem {
                        Label("Glasses", systemImage: "eyeglasses")
                    }

                SettingsView()
                    .tabItem {
                        Label("Settings", systemImage: "gearshape")
                    }
            }
            .tint(Color.jBlue)
            .toolbarBackground(Color.jCard, for: .tabBar)
            .toolbarBackground(.visible, for: .tabBar)
        }
    }
}

struct GlassesStatusView: View {
    @EnvironmentObject var glassesManager: GlassesManager

    var body: some View {
        NavigationStack {
            ZStack {
                Color.jBg.ignoresSafeArea()
                List {
                    Section {
                        statusRow(label: "SESSION", value: glassesManager.sessionState.displayName)
                        statusRow(label: "DISPLAY", value: glassesManager.displayState.displayName)
                        HStack {
                            Text("STATUS")
                                .font(.system(size: 12, design: .monospaced))
                                .tracking(1)
                                .foregroundColor(.jBlueDim)
                            Spacer()
                            Circle()
                                .fill(glassesManager.isConnected ? Color.jGreen : Color.jBorder)
                                .frame(width: 8, height: 8)
                                .shadow(color: glassesManager.isConnected ? .jGreen : .clear, radius: 4)
                        }
                        .listRowBackground(Color.jCard)
                    } header: { sectionHeader("CONNECTION") }

                    if let error = glassesManager.errorMessage {
                        Section {
                            Text(error)
                                .font(.system(size: 12, design: .monospaced))
                                .foregroundColor(Color(hex: "cc3344"))
                                .listRowBackground(Color.jCard)
                        } header: { sectionHeader("ERROR") }
                    }

                    Section {
                        actionButton(label: "RECONNECT", disabled: glassesManager.isConnected) {
                            Task { await glassesManager.reconnect() }
                        }
                        actionButton(label: "SHOW IDLE HUD", disabled: !glassesManager.isConnected) {
                            Task { try? await glassesManager.showIdleHUD() }
                        }
                    } header: { sectionHeader("CONTROLS") }
                }
                .scrollContentBackground(.hidden)
                .background(Color.jBg)
                .listRowSeparatorTint(Color.jBorder)
            }
            .navigationTitle("")
            .navigationBarTitleDisplayMode(.inline)
            .toolbar {
                ToolbarItem(placement: .principal) {
                    Text("GLASSES")
                        .font(.system(size: 13, weight: .bold, design: .monospaced))
                        .tracking(6)
                        .foregroundColor(.jBlue)
                }
            }
            .toolbarBackground(Color.jCard, for: .navigationBar)
            .toolbarBackground(.visible, for: .navigationBar)
        }
    }

    private func sectionHeader(_ title: String) -> some View {
        Text(title)
            .font(.system(size: 10, weight: .bold, design: .monospaced))
            .tracking(3)
            .foregroundColor(.jBlue.opacity(0.7))
    }

    private func statusRow(label: String, value: String) -> some View {
        HStack {
            Text(label)
                .font(.system(size: 12, design: .monospaced))
                .tracking(1)
                .foregroundColor(.jBlueDim)
            Spacer()
            Text(value)
                .font(.system(size: 12, design: .monospaced))
                .foregroundColor(.jText)
        }
        .listRowBackground(Color.jCard)
    }

    private func actionButton(label: String, disabled: Bool, action: @escaping () -> Void) -> some View {
        Button(action: action) {
            Text(label)
                .font(.system(size: 12, weight: .semibold, design: .monospaced))
                .tracking(2)
                .foregroundColor(disabled ? .jBlueDim.opacity(0.4) : .jBlue)
                .frame(maxWidth: .infinity, alignment: .center)
        }
        .disabled(disabled)
        .listRowBackground(Color.jCard)
    }
}
