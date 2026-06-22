import SwiftUI

struct ChatView: View {
    @EnvironmentObject var glassesManager: GlassesManager
    @EnvironmentObject var chatViewModel: ChatViewModel
    @State private var inputText = ""
    @State private var isRecording = false
    @FocusState private var fieldFocused: Bool

    var body: some View {
        NavigationStack {
            VStack(spacing: 0) {
                messageList
                Divider()
                inputBar
            }
            .navigationTitle("Jarvis")
            .navigationBarTitleDisplayMode(.inline)
            .toolbar {
                ToolbarItem(placement: .topBarTrailing) {
                    glassesStatusIndicator
                }
            }
        }
        .onAppear {
            chatViewModel.glassesManager = glassesManager
        }
        .onReceive(NotificationCenter.default.publisher(for: .jarvisActivateWake)) { _ in
            Task { await chatViewModel.sendVoice() }
        }
    }

    // MARK: Subviews

    private var messageList: some View {
        ScrollViewReader { proxy in
            ScrollView {
                LazyVStack(alignment: .leading, spacing: 12) {
                    ForEach(chatViewModel.messages) { message in
                        MessageBubble(message: message)
                            .id(message.id)
                    }
                }
                .padding(.horizontal)
                .padding(.top, 8)
            }
            .onChange(of: chatViewModel.messages.count) {
                if let last = chatViewModel.messages.last {
                    withAnimation { proxy.scrollTo(last.id, anchor: .bottom) }
                }
            }
        }
    }

    private var inputBar: some View {
        HStack(spacing: 10) {
            // Camera / glasses photo button
            Button {
                Task {
                    if let photoData = try? await glassesManager.capturePhoto() {
                        await chatViewModel.sendImage(photoData)
                    }
                }
            } label: {
                Image(systemName: "camera.fill")
                    .foregroundStyle(glassesManager.isConnected ? .blue : .gray)
            }
            .disabled(!glassesManager.isConnected || chatViewModel.isProcessing)

            // Text field
            TextField("Message Jarvis...", text: $inputText, axis: .vertical)
                .textFieldStyle(.roundedBorder)
                .lineLimit(1...4)
                .focused($fieldFocused)
                .onSubmit { submitText() }

            // PTT mic button or send button
            if inputText.isEmpty {
                Button {
                    Task { await chatViewModel.sendVoice() }
                } label: {
                    Image(systemName: isRecording ? "stop.circle.fill" : "mic.fill")
                        .foregroundStyle(isRecording ? .red : .blue)
                        .contentTransition(.symbolEffect(.replace))
                }
                .disabled(chatViewModel.isProcessing)
            } else {
                Button(action: submitText) {
                    Image(systemName: "arrow.up.circle.fill")
                        .foregroundStyle(.blue)
                }
                .disabled(chatViewModel.isProcessing)
            }
        }
        .padding(.horizontal)
        .padding(.vertical, 8)
        .background(Color(.systemBackground))
    }

    private var glassesStatusIndicator: some View {
        Circle()
            .fill(glassesManager.isConnected ? Color.green : Color.red)
            .frame(width: 10, height: 10)
    }

    private func submitText() {
        let text = inputText.trimmingCharacters(in: .whitespacesAndNewlines)
        guard !text.isEmpty else { return }
        inputText = ""
        fieldFocused = false
        Task { await chatViewModel.sendText(text) }
    }
}

// MARK: - Message bubble

struct MessageBubble: View {
    let message: ChatMessage

    var body: some View {
        HStack {
            if message.role == .user { Spacer() }

            Group {
                if message.isLoading {
                    ProgressView()
                        .padding(12)
                } else {
                    Text(message.text)
                        .padding(.horizontal, 14)
                        .padding(.vertical, 10)
                }
            }
            .background(message.role == .user ? Color.blue : Color(.secondarySystemBackground))
            .foregroundStyle(message.role == .user ? .white : .primary)
            .clipShape(RoundedRectangle(cornerRadius: 18))

            if message.role == .jarvis { Spacer() }
        }
    }
}
