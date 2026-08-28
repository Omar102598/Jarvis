import PhotosUI
import SwiftUI

struct SettingsView: View {
    @State private var serverURL = JarvisConfig.serverURL
    @State private var apiKey    = JarvisConfig.apiKey
    @State private var saved     = false
    @State private var urlError: String?
    @AppStorage("devMode") private var devMode = false
    @StateObject private var location = LocationManager.shared

    // Face enrollment (selfie → Sentry camera recognition)
    @AppStorage("faceName") private var faceName = ""
    @State private var facePhotos: [PhotosPickerItem] = []
    @State private var faceStatus = ""
    @State private var enrolling  = false

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
                        if let urlError {
                            Text(urlError)
                                .font(.system(size: 11, design: .monospaced))
                                .foregroundColor(.red)
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

                        sectionHeader("HOME LOCATION")
                        Text("Set your home so Jarvis can greet you and set the scene when you arrive — the reliable signal a single indoor camera can't give.")
                            .font(.system(size: 11))
                            .foregroundColor(.jBlueDim)
                        HStack {
                            Text(location.status.uppercased())
                                .font(.system(size: 12, weight: .semibold, design: .monospaced))
                                .foregroundColor(location.homeSet ? .jGreen : .jBlueDim)
                            Spacer()
                            Button("SET CURRENT LOCATION AS HOME") {
                                location.setHomeToCurrentLocation()
                            }
                            .font(.system(size: 11, weight: .bold, design: .monospaced))
                            .foregroundColor(.jBlue)
                        }

                        sectionHeader("FACE RECOGNITION")
                        Text("Teach Jarvis what you look like — pick 3-5 clear selfies (face-on, good light). The cameras use this to tell you apart from strangers; re-enroll any time to replace.")
                            .font(.system(size: 11))
                            .foregroundColor(.jBlueDim)
                        fieldRow(label: "NAME") {
                            TextField("omar", text: $faceName)
                                .textInputAutocapitalization(.never)
                                .autocorrectionDisabled()
                        }
                        PhotosPicker(selection: $facePhotos, maxSelectionCount: 5,
                                     matching: .images) {
                            Text(enrolling ? "ENROLLING..." : "SELECT SELFIES & ENROLL")
                                .font(.system(size: 12, weight: .bold, design: .monospaced))
                                .tracking(2)
                                .foregroundColor(.black)
                                .frame(maxWidth: .infinity)
                                .padding(.vertical, 12)
                                .background(faceName.isEmpty || enrolling
                                            ? Color.jBlueDim : Color.jBlue)
                                .cornerRadius(8)
                        }
                        .disabled(faceName.isEmpty || enrolling)
                        .onChange(of: facePhotos) { items in
                            guard !items.isEmpty else { return }
                            Task { await enrollFace() }
                        }
                        if !faceStatus.isEmpty {
                            Text(faceStatus)
                                .font(.system(size: 11, weight: .semibold, design: .monospaced))
                                .foregroundColor(faceStatus.hasPrefix("ENROLLED")
                                                 ? .jGreen : .jBlueDim)
                        }

                        sectionHeader("DEVELOPER")
                        Toggle(isOn: $devMode) {
                            Text("DEV MODE")
                                .font(.system(size: 13, weight: .semibold, design: .monospaced))
                                .foregroundColor(.jText)
                        }
                        .tint(.jBlue)
                        Text("Adds an Activity tab showing every agent's tool calls, reasoning, and findings in order — like watching Jarvis think.")
                            .font(.system(size: 11))
                            .foregroundColor(.jBlueDim)

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
        // Reject a URL that can't be parsed rather than storing it and letting
        // every later request fail (it used to crash on a force-unwrap).
        let cleaned = JarvisConfig.normalized(serverURL)
        guard URL(string: cleaned + "/health") != nil, cleaned.count > "http://".count else {
            urlError = "Not a valid URL. Expected something like http://host:8080"
            return
        }
        urlError = nil
        serverURL = cleaned
        JarvisConfig.serverURL = cleaned
        JarvisConfig.apiKey    = apiKey
        saved = true
        DispatchQueue.main.asyncAfter(deadline: .now() + 2) { saved = false }
    }

    private func enrollFace() async {
        enrolling = true
        faceStatus = "PROCESSING \(facePhotos.count) PHOTO(S)..."
        var images: [String] = []
        for item in facePhotos {
            if let data = try? await item.loadTransferable(type: Data.self),
               let ui = UIImage(data: data) {
                images.append(resizedJPEGBase64(ui))
            }
        }
        facePhotos = []
        guard !images.isEmpty else {
            faceStatus = "COULD NOT READ PHOTOS"
            enrolling = false
            return
        }
        do {
            let r = try await JarvisClient.shared.enrollFace(
                name: faceName.trimmingCharacters(in: .whitespaces).lowercased(),
                imagesB64: images)
            faceStatus = r.enrolled
                ? "ENROLLED \(faceName.uppercased()) (\(r.samples)/\(r.of) PHOTOS USABLE)"
                : "SAMPLES SAVED (\(r.samples)) — NOT FINALIZED"
        } catch {
            faceStatus = "FAILED — TRY CLOSER, WELL-LIT, FACE-ON SELFIES"
        }
        enrolling = false
    }

    /// Embedding quality doesn't need 12MP — cap the long edge so uploads
    /// stay small (the detector runs at 640px anyway).
    private func resizedJPEGBase64(_ img: UIImage) -> String {
        let maxDim: CGFloat = 1280
        let scale = min(1, maxDim / max(img.size.width, img.size.height))
        let size = CGSize(width: img.size.width * scale,
                          height: img.size.height * scale)
        let resized = UIGraphicsImageRenderer(size: size).image { _ in
            img.draw(in: CGRect(origin: .zero, size: size))
        }
        return (resized.jpegData(compressionQuality: 0.85) ?? Data())
            .base64EncodedString()
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
