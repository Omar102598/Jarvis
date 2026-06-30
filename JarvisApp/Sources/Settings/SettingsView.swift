import SwiftUI

struct SettingsView: View {
    @State private var serverURL = JarvisConfig.serverURL
    @State private var apiKey    = JarvisConfig.apiKey
    @State private var saved     = false

    var body: some View {
        NavigationStack {
            ZStack {
                Color.jBg.ignoresSafeArea()
                ScrollView {
                    VStack(alignment: .leading, spacing: 28) {
                        sectionHeader("CONNECTION")
                        fieldRow(label: "SERVER URL") {
                            TextField("http://192.168.1.100:8080", text: $serverURL)
                                .textInputAutocapitalization(.never)
                                .autocorrectionDisabled()
                                .keyboardType(.URL)
                        }
                        fieldRow(label: "API KEY") {
                            SecureField("Optional", text: $apiKey)
                        }

                        Button(action: save) {
                            Text("SAVE CONFIGURATION")
                                .font(.system(size: 13, weight: .bold, design: .monospaced))
                                .tracking(2)
                                .foregroundColor(.black)
                                .frame(maxWidth: .infinity)
                                .padding(.vertical, 14)
                                .background(serverURL.isEmpty ? Color.jBlueDim : Color.jBlue)
                                .cornerRadius(8)
                                .shadow(color: .jBlue.opacity(serverURL.isEmpty ? 0 : 0.4), radius: 8)
                        }
                        .disabled(serverURL.isEmpty)
                        .padding(.top, 4)

                        sectionHeader("ABOUT")
                        infoRow(label: "VERSION",   value: "1.0")
                        infoRow(label: "META SDK",  value: "DAT 0.7")

                        NavigationLink(destination: SetupGuideView()) {
                            HStack {
                                Text("SETUP GUIDE")
                                    .font(.system(size: 13, design: .monospaced))
                                    .tracking(1)
                                    .foregroundColor(.jBlue)
                                Spacer()
                                Image(systemName: "chevron.right")
                                    .font(.system(size: 11, weight: .semibold))
                                    .foregroundColor(.jBlueDim)
                            }
                            .padding(.horizontal, 16)
                            .padding(.vertical, 14)
                            .background(Color.jCard)
                            .overlay(
                                RoundedRectangle(cornerRadius: 8)
                                    .stroke(Color.jBorder, lineWidth: 1)
                            )
                            .cornerRadius(8)
                        }
                    }
                    .padding(.horizontal, 20)
                    .padding(.top, 24)
                    .padding(.bottom, 40)
                }
            }
            .navigationTitle("")
            .navigationBarTitleDisplayMode(.inline)
            .toolbar {
                ToolbarItem(placement: .principal) {
                    Text("SETTINGS")
                        .font(.system(size: 13, weight: .bold, design: .monospaced))
                        .tracking(6)
                        .foregroundColor(.jBlue)
                }
            }
            .toolbarBackground(Color.jCard, for: .navigationBar)
            .toolbarBackground(.visible, for: .navigationBar)
            .overlay(alignment: .bottom) {
                if saved {
                    Text("CONFIGURATION SAVED")
                        .font(.system(size: 11, weight: .bold, design: .monospaced))
                        .tracking(2)
                        .foregroundColor(.black)
                        .padding(.horizontal, 20)
                        .padding(.vertical, 10)
                        .background(Color.jGreen)
                        .clipShape(Capsule())
                        .shadow(color: .jGreen.opacity(0.5), radius: 8)
                        .padding(.bottom, 24)
                        .transition(.move(edge: .bottom).combined(with: .opacity))
                }
            }
            .animation(.spring, value: saved)
        }
    }

    private func save() {
        JarvisConfig.serverURL = serverURL
        JarvisConfig.apiKey    = apiKey
        saved = true
        DispatchQueue.main.asyncAfter(deadline: .now() + 2) { saved = false }
    }

    private func sectionHeader(_ title: String) -> some View {
        Text(title)
            .font(.system(size: 10, weight: .bold, design: .monospaced))
            .tracking(3)
            .foregroundColor(.jBlue.opacity(0.7))
    }

    private func fieldRow<F: View>(label: String, @ViewBuilder field: () -> F) -> some View {
        VStack(alignment: .leading, spacing: 8) {
            Text(label)
                .font(.system(size: 10, weight: .semibold, design: .monospaced))
                .tracking(2)
                .foregroundColor(.jBlueDim)
            field()
                .font(.system(size: 14, design: .monospaced))
                .foregroundColor(.jText)
                .tint(.jBlue)
                .padding(.horizontal, 16)
                .padding(.vertical, 13)
                .background(Color.jCard)
                .overlay(
                    RoundedRectangle(cornerRadius: 8)
                        .stroke(Color.jBorder, lineWidth: 1)
                )
                .cornerRadius(8)
        }
    }

    private func infoRow(label: String, value: String) -> some View {
        HStack {
            Text(label)
                .font(.system(size: 13, design: .monospaced))
                .foregroundColor(.jBlueDim)
            Spacer()
            Text(value)
                .font(.system(size: 13, design: .monospaced))
                .foregroundColor(.jText)
        }
        .padding(.horizontal, 16)
        .padding(.vertical, 14)
        .background(Color.jCard)
        .overlay(
            RoundedRectangle(cornerRadius: 8)
                .stroke(Color.jBorder, lineWidth: 1)
        )
        .cornerRadius(8)
    }
}

struct SetupGuideView: View {
    var body: some View {
        ZStack {
            Color.jBg.ignoresSafeArea()
            List {
                guideSection("PREREQUISITES") {
                    guideItem("Meta Ray-Ban Display glasses (firmware v125+)")
                    guideItem("Meta AI app v272+ on this iPhone")
                    guideItem("Developer Mode enabled on glasses")
                    guideItem("Jarvis server running on your Mac")
                }
                guideSection("FIRST LAUNCH") {
                    guideItem("Open this app — Meta AI will open to request registration approval.")
                    guideItem("Grant camera permission when prompted.")
                    guideItem("Set your server URL in Settings (Mac LAN IP, port 8080).")
                    guideItem("Return here — the glasses icon turns green when connected.")
                }
                guideSection("SIRI") {
                    guideItem("Say \"Hey Siri, ask Jarvis [your question]\" to query hands-free.")
                }
            }
            .scrollContentBackground(.hidden)
            .background(Color.jBg)
        }
        .navigationTitle("")
        .navigationBarTitleDisplayMode(.inline)
        .toolbar {
            ToolbarItem(placement: .principal) {
                Text("SETUP GUIDE")
                    .font(.system(size: 13, weight: .bold, design: .monospaced))
                    .tracking(4)
                    .foregroundColor(.jBlue)
            }
        }
        .toolbarBackground(Color.jCard, for: .navigationBar)
        .toolbarBackground(.visible, for: .navigationBar)
    }

    private func guideSection<C: View>(_ title: String, @ViewBuilder content: () -> C) -> some View {
        Section {
            content()
        } header: {
            Text(title)
                .font(.system(size: 10, weight: .bold, design: .monospaced))
                .tracking(3)
                .foregroundColor(.jBlue.opacity(0.7))
        }
        .listRowBackground(Color.jCard)
    }

    private func guideItem(_ text: String) -> some View {
        Text(text)
            .font(.system(size: 13))
            .foregroundColor(.jText)
            .listRowSeparatorTint(Color.jBorder)
    }
}
