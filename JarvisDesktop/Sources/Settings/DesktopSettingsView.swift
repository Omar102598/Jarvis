import SwiftUI
import JarvisShared

struct DesktopSettingsView: View {
    @EnvironmentObject var serviceManager: ServiceManager

    @State private var serverURL    = JarvisConfig.serverURL
    @State private var apiKey       = JarvisConfig.apiKey
    @State private var macBridgeURL = JarvisConfig.macBridgeURL
    @State private var repoPath     = JarvisConfig.repoPath
    @State private var saved        = false

    var body: some View {
        Form {
            Section("Jarvis Server") {
                LabeledContent("Server URL") {
                    TextField("http://192.168.1.100:8080", text: $serverURL)
                        .frame(width: 280)
                }
                LabeledContent("API Key") {
                    SecureField("optional", text: $apiKey)
                        .frame(width: 280)
                }
            }

            Section("Local Services") {
                LabeledContent("mac_bridge URL") {
                    TextField("http://localhost:7777", text: $macBridgeURL)
                        .frame(width: 280)
                }
                LabeledContent("Repo Path") {
                    TextField("auto-detect", text: $repoPath)
                        .frame(width: 280)
                }
                Text("Leave repo path empty to auto-detect from the app bundle location.")
                    .font(.caption)
                    .foregroundColor(.secondary)
            }

            Section("Service Status") {
                statusRow("mac_bridge", state: serviceManager.macBridgeState)
                statusRow("Docker Services", state: serviceManager.dockerState)
                statusRow("Audio Pipeline", state: serviceManager.audioState)

                HStack {
                    Button("Start All Services") { Task { await serviceManager.startAll() } }
                    Button("Stop All Services") { Task { await serviceManager.stopAll() } }
                        .foregroundColor(.red)
                }
            }

            HStack {
                Spacer()
                if saved {
                    Label("Saved", systemImage: "checkmark.circle.fill")
                        .foregroundColor(.green)
                }
                Button("Save") { save() }
                    .buttonStyle(.borderedProminent)
            }
        }
        .formStyle(.grouped)
        .padding()
        .navigationTitle("Jarvis Settings")
    }

    private func save() {
        JarvisConfig.serverURL    = serverURL
        JarvisConfig.apiKey       = apiKey
        JarvisConfig.macBridgeURL = macBridgeURL
        JarvisConfig.repoPath     = repoPath
        saved = true
        Task {
            try? await Task.sleep(nanoseconds: 2_000_000_000)
            saved = false
        }
    }

    @ViewBuilder
    private func statusRow(_ name: String, state: ServiceManager.ServiceState) -> some View {
        LabeledContent(name) {
            HStack(spacing: 4) {
                Circle().fill(state.color).frame(width: 8, height: 8)
                Text(state.label).foregroundColor(.secondary)
            }
        }
    }
}
