import SwiftUI
import PhotosUI

struct ChatView: View {
    @EnvironmentObject var glassesManager: GlassesManager
    @EnvironmentObject var chatViewModel: ChatViewModel
    @State private var inputText = ""
    @State private var isRecording = false
    @State private var micPulse: CGFloat = 1.0
    @State private var showTimeline = false
    @State private var mediaItem: PhotosPickerItem?
    @State private var showMediaPicker = false
    @State private var showSourceChoice = false
    @State private var showCamera = false
    @FocusState private var fieldFocused: Bool

    var body: some View {
        ZStack {
            Color.jBg.ignoresSafeArea()
            scanLine
            VStack(spacing: 0) {
                header
                if showTimeline && !chatViewModel.toolEvents.isEmpty {
                    ToolTimelinePanel(events: chatViewModel.toolEvents)
                        .transition(.move(edge: .top).combined(with: .opacity))
                }
                messageList
                if chatViewModel.isPlayingAudio {
                    audioWaveBar
                        .transition(.move(edge: .bottom).combined(with: .opacity))
                }
                inputBar
            }
        }
        .animation(.easeInOut(duration: 0.25), value: showTimeline)
        .animation(.easeInOut(duration: 0.3), value: chatViewModel.isPlayingAudio)
        .onAppear {
            chatViewModel.glassesManager = glassesManager
            chatViewModel.startToolPolling()
            chatViewModel.startToolStream()
        }
        .onDisappear { chatViewModel.stopToolStream() }
        .onReceive(NotificationCenter.default.publisher(for: .jarvisActivateWake)) { _ in
            Task { await chatViewModel.sendVoice() }
        }
    }

    // MARK: - Header

    private var header: some View {
        ZStack {
            HStack {
                statusDot
                Spacer()
                if !chatViewModel.toolEvents.isEmpty {
                    Button {
                        withAnimation { showTimeline.toggle() }
                    } label: {
                        HStack(spacing: 5) {
                            Image(systemName: "terminal")
                                .font(.system(size: 11, weight: .medium))
                            Text("\(chatViewModel.toolEvents.count)")
                                .font(.system(size: 11, weight: .semibold, design: .monospaced))
                        }
                        .foregroundColor(showTimeline ? .jBlue : .jBlueDim)
                        .padding(.horizontal, 8)
                        .padding(.vertical, 4)
                        .background(Color.jBorder.opacity(showTimeline ? 0.8 : 0.4))
                        .cornerRadius(6)
                    }
                }
            }
            Text("J·A·R·V·I·S")
                .font(.system(size: 17, weight: .bold, design: .monospaced))
                .tracking(8)
                .foregroundColor(.jBlue)
                .shadow(color: .jBlue.opacity(0.8), radius: 8)
                .shadow(color: .jBlue.opacity(0.4), radius: 16)
        }
        .padding(.horizontal, 20)
        .padding(.vertical, 14)
        .background(Color.jCard)
        .overlay(Rectangle().frame(height: 1).foregroundColor(.jBorder), alignment: .bottom)
        .hudCorner()
    }

    private var statusDot: some View {
        Circle()
            .fill(glassesManager.isConnected ? Color.jGreen : Color.jBlueDim)
            .frame(width: 7, height: 7)
            .shadow(color: glassesManager.isConnected ? .jGreen : .jBlueDim, radius: 4)
    }

    // MARK: - Scan line

    private var scanLine: some View {
        GeometryReader { _ in
            ScanLineView()
        }
        .allowsHitTesting(false)
    }

    // MARK: - Message list

    private var messageList: some View {
        ScrollViewReader { proxy in
            ScrollView {
                if chatViewModel.messages.isEmpty {
                    emptyStateView
                        .frame(maxWidth: .infinity)
                        .padding(.top, 60)
                } else {
                    LazyVStack(alignment: .leading, spacing: 10) {
                        ForEach(chatViewModel.messages) { message in
                            MessageBubble(message: message)
                                .id(message.id)
                        }
                    }
                    .padding(.horizontal, 16)
                    .padding(.vertical, 12)
                }
            }
            .onChange(of: chatViewModel.messages.count) {
                if let last = chatViewModel.messages.last {
                    withAnimation { proxy.scrollTo(last.id, anchor: .bottom) }
                }
            }
        }
    }

    // MARK: - Empty state

    private var emptyStateView: some View {
        VStack(spacing: 24) {
            IdleOrbView()
                .frame(width: 120, height: 120)
            VStack(spacing: 6) {
                HStack(spacing: 6) {
                    Circle()
                        .fill(Color.jGreen)
                        .frame(width: 6, height: 6)
                        .shadow(color: .jGreen, radius: 4)
                    Text("ONLINE")
                        .font(.system(size: 10, weight: .bold, design: .monospaced))
                        .tracking(4)
                        .foregroundColor(.jGreen)
                }
                Text("Say \"Hey Jarvis\" or type below")
                    .font(.system(size: 13, design: .monospaced))
                    .foregroundColor(.jBlueDim)
                    .multilineTextAlignment(.center)
            }
        }
        .padding(.horizontal, 40)
    }

    // MARK: - Audio wave bar

    private var audioWaveBar: some View {
        HStack(spacing: 12) {
            Image(systemName: "waveform")
                .font(.system(size: 12))
                .foregroundColor(.jBlueDim)
            Text("RESPONDING")
                .font(.system(size: 10, weight: .semibold, design: .monospaced))
                .tracking(2)
                .foregroundColor(.jBlueDim)
            Spacer()
            AudioWaveView(isAnimating: chatViewModel.isPlayingAudio)
        }
        .padding(.horizontal, 16)
        .padding(.vertical, 8)
        .background(Color.jCard)
        .overlay(Rectangle().frame(height: 1).foregroundColor(.jBorder), alignment: .top)
    }

    // MARK: - Input bar

    private var inputBar: some View {
        VStack(spacing: 0) {
            Rectangle().fill(Color.jBorder).frame(height: 1)
            HStack(spacing: 12) {
                // Glasses when they're connected; otherwise the phone's own
                // library. Previously this was hard-disabled without glasses,
                // which made image/video analysis unreachable on the phone.
                Button {
                    if glassesManager.isConnected {
                        Task {
                            if let photoData = try? await glassesManager.capturePhoto() {
                                await chatViewModel.sendImage(photoData)
                            }
                        }
                    } else {
                        // Without glasses, ask rather than assume: shooting a
                        // clip now and picking an old one are different intents.
                        showSourceChoice = true
                    }
                } label: {
                    Image(systemName: glassesManager.isConnected ? "camera" : "photo.on.rectangle")
                        .font(.system(size: 16, weight: .medium))
                        .foregroundColor(.jBlueDim)
                }
                .disabled(chatViewModel.isProcessing)
                .photosPicker(isPresented: $showMediaPicker, selection: $mediaItem,
                              matching: .any(of: [.videos, .images]))
                .confirmationDialog("Add media", isPresented: $showSourceChoice,
                                    titleVisibility: .visible) {
                    if CameraPicker.isAvailable {
                        Button("Take Photo or Video") { showCamera = true }
                    }
                    Button("Choose from Library") { showMediaPicker = true }
                    Button("Cancel", role: .cancel) {}
                }
                .fullScreenCover(isPresented: $showCamera) {
                    CameraPicker { capture in
                        Task {
                            switch capture {
                            case .image(let data): await chatViewModel.sendImage(data)
                            case .video(let data): await chatViewModel.sendVideo(data)
                            }
                        }
                    }
                    .ignoresSafeArea()
                }
                .onChange(of: mediaItem) { _, item in
                    guard let item else { return }
                    Task {
                        defer { mediaItem = nil }
                        guard let data = try? await item.loadTransferable(type: Data.self) else { return }
                        // A video's frames get sampled server-side (or handed to
                        // Gemini); an image goes down the existing image path.
                        if item.supportedContentTypes.contains(where: { $0.conforms(to: .movie) }) {
                            await chatViewModel.sendVideo(data)
                        } else {
                            await chatViewModel.sendImage(data)
                        }
                    }
                }

                ZStack(alignment: .leading) {
                    if inputText.isEmpty {
                        Text("Message Jarvis...")
                            .font(.system(size: 14, design: .monospaced))
                            .foregroundColor(.jBlueDim.opacity(0.5))
                            .padding(.horizontal, 12)
                    }
                    TextField("", text: $inputText, axis: .vertical)
                        .font(.system(size: 14, design: .monospaced))
                        .foregroundColor(.jText)
                        .tint(.jBlue)
                        .lineLimit(1...4)
                        .focused($fieldFocused)
                        .padding(.horizontal, 12)
                        .padding(.vertical, 8)
                        .onSubmit { submitText() }
                }
                .background(Color.jCard)
                .overlay(
                    RoundedRectangle(cornerRadius: 8)
                        .stroke(fieldFocused ? Color.jBlue.opacity(0.6) : Color.jBorder, lineWidth: 1)
                )
                .cornerRadius(8)

                if inputText.isEmpty {
                    micButton
                } else {
                    sendButton
                }
            }
            .padding(.horizontal, 16)
            .padding(.vertical, 12)
            .background(Color.jCard)
        }
    }

    private var micButton: some View {
        Button {
            Task {
                isRecording = true
                withAnimation(.easeInOut(duration: 0.5).repeatForever(autoreverses: true)) {
                    micPulse = 1.12
                }
                await chatViewModel.sendVoice()
                isRecording = false
                micPulse = 1.0
            }
        } label: {
            ZStack {
                Circle()
                    .fill(isRecording ? Color.jGold : Color.jGreen)
                    .frame(width: 44, height: 44)
                    .shadow(color: isRecording ? .jGold.opacity(0.6) : .jGreen.opacity(0.6), radius: 8)
                    .scaleEffect(isRecording ? micPulse : 1.0)
                Image(systemName: isRecording ? "waveform" : "mic.fill")
                    .font(.system(size: 16, weight: .semibold))
                    .foregroundColor(.black)
            }
        }
        .disabled(chatViewModel.isProcessing && !isRecording)
    }

    private var sendButton: some View {
        Button(action: submitText) {
            ZStack {
                Circle()
                    .fill(Color.jBlue)
                    .frame(width: 44, height: 44)
                    .shadow(color: .jBlue.opacity(0.5), radius: 6)
                Image(systemName: "arrow.up")
                    .font(.system(size: 16, weight: .bold))
                    .foregroundColor(.black)
            }
        }
        .disabled(chatViewModel.isProcessing)
    }

    private func submitText() {
        let text = inputText.trimmingCharacters(in: .whitespacesAndNewlines)
        guard !text.isEmpty else { return }
        inputText = ""
        fieldFocused = false
        Task { await chatViewModel.sendText(text) }
    }
}

// MARK: - Scan line (extracted for cleanliness)

private struct ScanLineView: View {
    @State private var offset: CGFloat = -UIScreen.main.bounds.height

    var body: some View {
        Rectangle()
            .fill(Color.jBlue.opacity(0.35))
            .frame(height: 1)
            .offset(y: offset)
            .onAppear {
                let h = UIScreen.main.bounds.height
                offset = -h
                withAnimation(.linear(duration: 7).repeatForever(autoreverses: false)) {
                    offset = h
                }
            }
    }
}

// MARK: - Idle orb (empty state animation)

private struct IdleOrbView: View {
    @State private var pulse = false

    var body: some View {
        ZStack {
            ForEach(0..<3) { i in
                Circle()
                    .stroke(Color.jBlue.opacity(0.15 - Double(i) * 0.03), lineWidth: 1)
                    .frame(width: CGFloat(60 + i * 22), height: CGFloat(60 + i * 22))
                    .scaleEffect(pulse ? 1.08 : 0.94)
                    .animation(
                        .easeInOut(duration: 2.0).repeatForever(autoreverses: true)
                            .delay(Double(i) * 0.35),
                        value: pulse
                    )
            }
            Circle()
                .fill(
                    RadialGradient(
                        colors: [Color.jBlue.opacity(0.15), Color.jBg],
                        center: .center, startRadius: 0, endRadius: 30
                    )
                )
                .frame(width: 54, height: 54)
                .overlay(
                    Circle().stroke(Color.jBlue.opacity(0.4), lineWidth: 1)
                )
            Text("J")
                .font(.system(size: 26, weight: .bold, design: .monospaced))
                .foregroundColor(.jBlue)
                .shadow(color: .jBlue.opacity(0.8), radius: 6)
        }
        .onAppear { pulse = true }
    }
}

// MARK: - Audio wave view

struct AudioWaveView: View {
    let isAnimating: Bool
    private let barCount = 6
    @State private var heights: [CGFloat] = Array(repeating: 3, count: 6)

    let timer = Timer.publish(every: 0.10, on: .main, in: .common).autoconnect()

    var body: some View {
        HStack(spacing: 2.5) {
            ForEach(0..<barCount, id: \.self) { i in
                RoundedRectangle(cornerRadius: 1.5)
                    .fill(Color.jBlue)
                    .shadow(color: .jBlue.opacity(0.4), radius: 2)
                    .frame(width: 2.5, height: heights[i])
                    .animation(.easeInOut(duration: 0.1), value: heights[i])
            }
        }
        .frame(height: 20)
        .onReceive(timer) { _ in
            if isAnimating {
                heights = (0..<barCount).map { _ in CGFloat.random(in: 3...20) }
            } else if heights.first != 3 {
                heights = Array(repeating: 3, count: barCount)
            }
        }
    }
}

// MARK: - Tool timeline panel

struct ToolTimelinePanel: View {
    let events: [ToolEvent]

    var body: some View {
        VStack(alignment: .leading, spacing: 0) {
            ScrollView(.vertical, showsIndicators: false) {
                VStack(alignment: .leading, spacing: 0) {
                    ForEach(Array(events.prefix(20).enumerated()), id: \.element.id) { idx, event in
                        ToolEventRow(event: event, isLast: idx == min(events.count, 20) - 1)
                    }
                }
                .padding(.vertical, 10)
                .padding(.horizontal, 16)
            }
            .frame(maxHeight: 200)
        }
        .background(Color.jCard.opacity(0.95))
        .overlay(Rectangle().frame(height: 1).foregroundColor(.jBorder), alignment: .bottom)
    }
}

private struct ToolEventRow: View {
    let event: ToolEvent
    let isLast: Bool

    private var dotColor: Color {
        switch event.status {
        case "done":    return .jGreen
        case "calling": return .jGold
        default:        return .jBlueDim
        }
    }

    private var timeString: String {
        guard let date = ISO8601DateFormatter().date(from: event.timestamp) else { return "" }
        let f = DateFormatter()
        f.dateFormat = "HH:mm:ss"
        return f.string(from: date)
    }

    var body: some View {
        HStack(alignment: .top, spacing: 10) {
            // Spine: dot + vertical line
            VStack(spacing: 0) {
                Circle()
                    .fill(dotColor)
                    .frame(width: 7, height: 7)
                    .shadow(color: dotColor.opacity(0.6), radius: 3)
                    .padding(.top, 3)
                if !isLast {
                    Rectangle()
                        .fill(Color.jBorder)
                        .frame(width: 1)
                        .frame(maxHeight: .infinity)
                }
            }
            .frame(width: 14)

            // Content
            VStack(alignment: .leading, spacing: 3) {
                HStack(spacing: 6) {
                    Text(event.tool)
                        .font(.system(size: 11, weight: .semibold, design: .monospaced))
                        .foregroundColor(dotColor)
                    Spacer()
                    Text(timeString)
                        .font(.system(size: 9, design: .monospaced))
                        .foregroundColor(.jBlueDim.opacity(0.4))
                }
                if !event.argsPreview.isEmpty {
                    Text(event.argsPreview)
                        .font(.system(size: 9, design: .monospaced))
                        .foregroundColor(.jBlueDim.opacity(0.5))
                        .lineLimit(2)
                }
                if !event.resultPreview.isEmpty && event.status == "done" {
                    Text(event.resultPreview)
                        .font(.system(size: 10))
                        .foregroundColor(.jText.opacity(0.5))
                        .lineLimit(3)
                }
            }
            .padding(.bottom, isLast ? 0 : 10)
        }
    }
}

// MARK: - Message bubble

struct MessageBubble: View {
    let message: ChatMessage

    var body: some View {
        HStack {
            if message.role == .user { Spacer(minLength: 60) }
            Group {
                if message.role == .user {
                    Text(message.text)
                        .font(.system(size: 14))
                        .foregroundColor(.black)
                        .padding(.horizontal, 14)
                        .padding(.vertical, 10)
                        .background(Color.jBlue)
                        .cornerRadius(12)
                } else {
                    jarvisBubble
                }
            }
            if message.role == .jarvis { Spacer(minLength: 60) }
        }
    }

    /// Tool calls render ABOVE the answer, the way Claude and ChatGPT show
    /// their work — and they appear DURING the turn, so a long multi-tool
    /// request shows what it is doing instead of a bare spinner.
    private var jarvisBubble: some View {
        VStack(alignment: .leading, spacing: 8) {
            if !message.toolCalls.isEmpty {
                ToolCallList(calls: message.toolCalls)
            }

            if message.isLoading {
                HStack(spacing: 8) {
                    ProgressView().tint(.jBlue).scaleEffect(0.8)
                    // Once tools are running, the spinner alone is ambiguous —
                    // name what is happening.
                    if !message.toolCalls.isEmpty {
                        Text("working…")
                            .font(.system(size: 11))
                            .foregroundColor(.jText.opacity(0.55))
                    }
                }
            } else {
                if !message.text.isEmpty {
                    Text(message.text)
                        .font(.system(size: 14))
                        .foregroundColor(.jText)
                        .textSelection(.enabled)
                }
                if let media = message.mediaURL, let url = URL(string: media) {
                    MediaCard(url: url,
                              isVideo: media.contains(".m3u8"),
                              cachedData: message.imageData)
                }
            }
        }
        .padding(.horizontal, 14)
        .padding(.vertical, 10)
        .background(Color.jCard)
        .overlay(
            RoundedRectangle(cornerRadius: 12)
                .stroke(Color.jBlue.opacity(0.35), lineWidth: 1)
        )
        .cornerRadius(12)
    }
}

// MARK: - Inline tool calls (Claude/ChatGPT style)

/// One row per tool call, tap to expand its input and result.
///
/// A row is keyed by the model's tool-call id, so a call resolves in place
/// from running to finished instead of appearing twice.
struct ToolCallList: View {
    let calls: [ToolEvent]

    var body: some View {
        VStack(alignment: .leading, spacing: 4) {
            ForEach(calls) { call in
                ToolCallRow(call: call)
            }
        }
    }
}

private struct ToolCallRow: View {
    let call: ToolEvent
    @State private var expanded = false

    private var displayName: String {
        call.tool.replacingOccurrences(of: "_", with: " ")
    }

    private var hasDetail: Bool {
        !call.argsPreview.isEmpty || !call.resultPreview.isEmpty
    }

    var body: some View {
        VStack(alignment: .leading, spacing: 6) {
            Button {
                withAnimation(.easeInOut(duration: 0.15)) { expanded.toggle() }
            } label: {
                HStack(spacing: 8) {
                    if call.isFinished {
                        Image(systemName: "checkmark.circle.fill")
                            .font(.system(size: 11))
                            .foregroundColor(.jGreen)
                    } else {
                        ProgressView()
                            .scaleEffect(0.55)
                            .tint(.jGold)
                            .frame(width: 12, height: 12)
                    }
                    Text(displayName)
                        .font(.system(size: 12, weight: .medium, design: .monospaced))
                        .foregroundColor(.jText.opacity(0.8))
                        .lineLimit(1)
                    Spacer(minLength: 4)
                    if hasDetail {
                        Image(systemName: expanded ? "chevron.up" : "chevron.down")
                            .font(.system(size: 9, weight: .semibold))
                            .foregroundColor(.jBlueDim)
                    }
                }
            }
            .buttonStyle(.plain)
            .disabled(!hasDetail)

            if expanded {
                if !call.argsPreview.isEmpty {
                    detail("input", call.argsPreview)
                }
                if !call.resultPreview.isEmpty {
                    detail("result", call.resultPreview)
                }
            }
        }
        .padding(.horizontal, 10)
        .padding(.vertical, 7)
        .background(Color.jBg.opacity(0.6))
        .overlay(
            RoundedRectangle(cornerRadius: 8)
                .stroke(Color.jBorder, lineWidth: 1)
        )
        .cornerRadius(8)
    }

    private func detail(_ label: String, _ text: String) -> some View {
        VStack(alignment: .leading, spacing: 2) {
            Text(label.uppercased())
                .font(.system(size: 9, weight: .semibold))
                .foregroundColor(.jBlueDim)
            Text(text)
                .font(.system(size: 11, design: .monospaced))
                .foregroundColor(.jText.opacity(0.75))
                .textSelection(.enabled)
                .fixedSize(horizontal: false, vertical: true)
        }
        .frame(maxWidth: .infinity, alignment: .leading)
    }
}

// MARK: - Tap-to-view media card (Ring snapshots / live HLS streams)

import AVKit

struct MediaCard: View {
    let url: URL
    let isVideo: Bool
    var cachedData: Data? = nil
    @State private var showViewer = false

    var body: some View {
        Group {
            if isVideo {
                HStack(spacing: 8) {
                    Image(systemName: "play.circle.fill").font(.system(size: 28))
                    Text("LIVE VIEW — tap to play")
                        .font(.system(size: 12, weight: .semibold, design: .monospaced))
                }
                .foregroundColor(.jBlue)
                .frame(maxWidth: .infinity, minHeight: 64)
                .background(Color.jBlue.opacity(0.08))
                .cornerRadius(10)
            } else if let data = cachedData, let ui = UIImage(data: data) {
                // Rendered from pinned bytes — cannot re-fetch or vanish.
                Image(uiImage: ui).resizable().scaledToFill()
                    .frame(maxWidth: 260, maxHeight: 160)
                    .clipped()
                    .cornerRadius(10)
            } else {
                AsyncImage(url: url) { phase in
                    switch phase {
                    case .success(let image):
                        image.resizable().scaledToFill()
                    case .failure:
                        Label("snapshot unavailable", systemImage: "video.slash")
                            .font(.system(size: 12, design: .monospaced))
                            .foregroundColor(.jBlueDim)
                    default:
                        ProgressView().tint(.jBlue)
                    }
                }
                .frame(maxWidth: 260, maxHeight: 160)
                .clipped()
                .cornerRadius(10)
            }
        }
        .onTapGesture { showViewer = true }
        .fullScreenCover(isPresented: $showViewer) {
            MediaViewer(url: url, isVideo: isVideo, cachedData: cachedData)
        }
    }
}

struct MediaViewer: View {
    let url: URL
    let isVideo: Bool
    var cachedData: Data? = nil
    @Environment(\.dismiss) private var dismiss
    @State private var player: AVPlayer? = nil

    var body: some View {
        ZStack(alignment: .topTrailing) {
            Color.black.ignoresSafeArea()
            if isVideo {
                VideoPlayer(player: player)
                    .ignoresSafeArea()
                    .onAppear {
                        let p = AVPlayer(url: url)
                        player = p
                        p.play()
                    }
                    .onDisappear { player?.pause() }
            } else if let data = cachedData, let ui = UIImage(data: data) {
                Image(uiImage: ui).resizable().scaledToFit()
            } else {
                AsyncImage(url: url) { phase in
                    if case .success(let image) = phase {
                        image.resizable().scaledToFit()
                    } else {
                        ProgressView().tint(.white)
                    }
                }
            }
            Button {
                dismiss()
            } label: {
                Image(systemName: "xmark.circle.fill")
                    .font(.system(size: 32))
                    .foregroundColor(.white.opacity(0.85))
                    .padding()
            }
        }
    }
}
