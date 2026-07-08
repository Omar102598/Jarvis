import SwiftUI

/// Dev-mode Activity tab — the unified agent event stream (tool / thinking /
/// finding) straight from Redis via the gateway. Zero LLM tokens; auto-refreshes
/// while visible. Mirrors the dashboard's DEV panel so you can watch Jarvis's
/// agents work step by step from your phone.
struct ActivityView: View {
    @State private var events: [JarvisClient.AgentEvent] = []
    @State private var errorText: String?
    @State private var timer: Timer?

    var body: some View {
        NavigationStack {
            ZStack {
                Color.jBg.ignoresSafeArea()
                if events.isEmpty && errorText == nil {
                    Text("No agent activity yet.\nTrigger an agent or wait for a scheduled run.")
                        .multilineTextAlignment(.center)
                        .font(.system(size: 12, design: .monospaced))
                        .foregroundColor(.jBlueDim)
                }
                List {
                    if let errorText {
                        Text(errorText)
                            .font(.system(size: 12, design: .monospaced))
                            .foregroundColor(Color(hex: "cc3344"))
                            .listRowBackground(Color.jCard)
                    }
                    ForEach(events) { e in
                        EventRow(event: e).listRowBackground(Color.jCard)
                    }
                }
                .scrollContentBackground(.hidden)
                .refreshable { await load() }
            }
            .navigationTitle("")
            .navigationBarTitleDisplayMode(.inline)
            .toolbar {
                ToolbarItem(placement: .principal) {
                    Text("ACTIVITY")
                        .font(.system(size: 13, weight: .bold, design: .monospaced))
                        .tracking(6)
                        .foregroundColor(.jBlue)
                }
            }
            .toolbarBackground(Color.jCard, for: .navigationBar)
            .toolbarBackground(.visible, for: .navigationBar)
        }
        .task { await load() }
        .onAppear {
            timer = Timer.scheduledTimer(withTimeInterval: 4, repeats: true) { _ in
                Task { await load() }
            }
        }
        .onDisappear { timer?.invalidate() }
    }

    private func load() async {
        do {
            events = try await JarvisClient.shared.fetchAgentEvents()
            errorText = nil
        } catch {
            errorText = "Couldn't reach Jarvis: \(error.localizedDescription)"
        }
    }
}

private struct EventRow: View {
    let event: JarvisClient.AgentEvent

    private var badgeColor: Color {
        switch event.kind {
        case "tool":     return .jBlue
        case "thinking": return .yellow
        case "finding":  return .jGreen
        default:         return .jBlueDim
        }
    }

    var body: some View {
        HStack(alignment: .top, spacing: 10) {
            VStack(spacing: 3) {
                Text(event.kind.uppercased())
                    .font(.system(size: 8, weight: .bold, design: .monospaced))
                    .foregroundColor(badgeColor)
                Text(event.agent)
                    .font(.system(size: 8, design: .monospaced))
                    .foregroundColor(.jBlueDim)
            }
            .frame(width: 62, alignment: .leading)
            Rectangle().fill(badgeColor).frame(width: 2)
            Text(event.text)
                .font(.system(size: 12))
                .foregroundColor(.jText)
        }
        .padding(.vertical, 2)
    }
}
