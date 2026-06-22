import SwiftUI

struct SettingsView: View {
    @State private var serverURL = JarvisConfig.serverURL
    @State private var apiKey = JarvisConfig.apiKey
    @State private var saved = false

    var body: some View {
        NavigationStack {
            Form {
                Section("Server") {
                    LabeledContent("URL") {
                        TextField("http://192.168.1.100:8080", text: $serverURL)
                            .textInputAutocapitalization(.never)
                            .autocorrectionDisabled()
                            .multilineTextAlignment(.trailing)
                            .keyboardType(.URL)
                    }
                    LabeledContent("API Key") {
                        SecureField("Optional", text: $apiKey)
                            .multilineTextAlignment(.trailing)
                    }
                }

                Section {
                    Button("Save") {
                        JarvisConfig.serverURL = serverURL
                        JarvisConfig.apiKey = apiKey
                        saved = true
                        DispatchQueue.main.asyncAfter(deadline: .now() + 2) { saved = false }
                    }
                    .disabled(serverURL.isEmpty)
                }

                Section("About") {
                    LabeledContent("Version", value: "1.0")
                    LabeledContent("SDK", value: "Meta DAT 0.7")
                    NavigationLink("Setup Guide") { SetupGuideView() }
                }
            }
            .navigationTitle("Settings")
            .overlay(alignment: .bottom) {
                if saved {
                    Text("Saved")
                        .padding(.horizontal, 20)
                        .padding(.vertical, 8)
                        .background(Color.green)
                        .foregroundStyle(.white)
                        .clipShape(Capsule())
                        .padding(.bottom, 20)
                        .transition(.move(edge: .bottom).combined(with: .opacity))
                }
            }
            .animation(.spring, value: saved)
        }
    }
}

struct SetupGuideView: View {
    var body: some View {
        List {
            Section("Prerequisites") {
                Text("1. Meta Ray-Ban Display glasses (firmware v125+)")
                Text("2. Meta AI app v272+ on this iPhone")
                Text("3. Developer Mode enabled on glasses")
                Text("4. Jarvis server running on your Mac")
            }
            Section("First Launch") {
                Text("1. Open this app — Meta AI will open to request registration approval.")
                Text("2. Grant camera permission when prompted.")
                Text("3. Set your server URL in Settings (your Mac's LAN IP, port 8080).")
                Text("4. Return here — the glasses icon turns green when connected.")
            }
            Section("Siri Integration") {
                Text("Say \"Hey Siri, ask Jarvis [your question]\" to query Jarvis hands-free without opening the app.")
            }
            Section("Known Issues") {
                Text("If connection fails with 'datAppOnTheGlassesUpdateRequired': restart Meta AI, re-pair glasses, and relaunch this app. (SDK bug #180)")
            }
        }
        .navigationTitle("Setup Guide")
    }
}
