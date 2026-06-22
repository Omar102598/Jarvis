import SwiftUI
import JarvisShared

// MARK: - Chat view model

@MainActor
final class DesktopChatViewModel: ObservableObject {
    @Published var messages: [ChatMessage] = []
    @Published var isProcessing = false
    @Published var errorMessage: String?

    private var wsTask: URLSessionWebSocketTask?

    func connectWebSocket() {
        wsTask = JarvisClient.shared.connectWebSocket { [weak self] payload in
            Task { @MainActor [weak self] in
                // Desktop shows incoming push payloads as chat messages
                guard let self, payload.type != .audioOnly else { return }
                let text = payload.body.isEmpty ? payload.title : "\(payload.title)\n\(payload.body)"
                self.messages.append(ChatMessage(role: .jarvis, text: text))
            }
        }
    }

    func sendText(_ text: String) async {
        guard !isProcessing else { return }
        messages.append(ChatMessage(role: .user, text: text))

        let loadingIdx = messages.count
        messages.append(ChatMessage(role: .jarvis, text: "", isLoading: true))
        isProcessing = true

        do {
            let response = try await JarvisClient.shared.askText(text, source: "mac")
            messages[loadingIdx].text = response.text
            messages[loadingIdx].isLoading = false
        } catch {
            messages[loadingIdx].text = "Error: \(error.localizedDescription)"
            messages[loadingIdx].isLoading = false
            errorMessage = error.localizedDescription
        }
        isProcessing = false
    }
}

// MARK: - Chat view

struct DesktopChatView: View {
    @EnvironmentObject var viewModel: DesktopChatViewModel
    @State private var inputText = ""
    @State private var scrollProxy: ScrollViewProxy?

    var body: some View {
        VStack(spacing: 0) {
            // Messages
            ScrollViewReader { proxy in
                ScrollView {
                    LazyVStack(alignment: .leading, spacing: 8) {
                        ForEach(viewModel.messages) { msg in
                            MessageBubble(message: msg)
                                .id(msg.id)
                        }
                    }
                    .padding(12)
                }
                .onAppear { scrollProxy = proxy }
                .onChange(of: viewModel.messages.count) { _ in
                    if let last = viewModel.messages.last {
                        withAnimation { proxy.scrollTo(last.id, anchor: .bottom) }
                    }
                }
            }

            Divider()

            // Input bar
            HStack(spacing: 8) {
                TextField("Ask Jarvis…", text: $inputText, axis: .vertical)
                    .textFieldStyle(.plain)
                    .lineLimit(1...5)
                    .onSubmit {
                        if !NSEvent.modifierFlags.contains(.shift) { send() }
                    }

                Button(action: send) {
                    Image(systemName: viewModel.isProcessing ? "arrow.up.circle" : "arrow.up.circle.fill")
                        .font(.title2)
                        .foregroundColor(inputText.trimmingCharacters(in: .whitespaces).isEmpty ? .secondary : .accentColor)
                }
                .buttonStyle(.plain)
                .disabled(inputText.trimmingCharacters(in: .whitespaces).isEmpty || viewModel.isProcessing)
                .keyboardShortcut(.return, modifiers: .command)
            }
            .padding(.horizontal, 12)
            .padding(.vertical, 8)
        }
        .navigationTitle("Jarvis")
        .task { viewModel.connectWebSocket() }
    }

    private func send() {
        let text = inputText.trimmingCharacters(in: .whitespaces)
        guard !text.isEmpty else { return }
        inputText = ""
        Task { await viewModel.sendText(text) }
    }
}

// MARK: - Message bubble

private struct MessageBubble: View {
    let message: ChatMessage

    var body: some View {
        HStack(alignment: .top) {
            if message.role == .user { Spacer(minLength: 40) }

            Group {
                if message.isLoading {
                    HStack(spacing: 6) {
                        ProgressView().scaleEffect(0.6)
                        Text("Thinking…").foregroundColor(.secondary)
                    }
                    .padding(.horizontal, 10)
                    .padding(.vertical, 7)
                } else {
                    Text(message.text)
                        .textSelection(.enabled)
                        .padding(.horizontal, 10)
                        .padding(.vertical, 7)
                }
            }
            .background(message.role == .user ? Color.accentColor : Color(.controlBackgroundColor))
            .foregroundColor(message.role == .user ? .white : .primary)
            .cornerRadius(12)

            if message.role == .jarvis { Spacer(minLength: 40) }
        }
    }
}
