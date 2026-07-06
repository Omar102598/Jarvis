import SwiftUI

/// Agents tab — every agent's latest report, fetched straight from Redis via
/// the gateway. No LLM involved: reading Apollo's workout or Walter's digest
/// here costs zero tokens. Fully dynamic — renders whatever agents exist.
struct AgentsView: View {
    @State private var agents: [JarvisClient.AgentFeedItem] = []
    @State private var errorText: String?

    var body: some View {
        NavigationStack {
            ZStack {
                Color.jBg.ignoresSafeArea()
                List {
                    if let errorText {
                        Text(errorText)
                            .font(.system(size: 12, design: .monospaced))
                            .foregroundColor(.jBlueDim)
                            .listRowBackground(Color.jCard)
                    }
                    ForEach(agents) { agent in
                        NavigationLink(destination: AgentReportView(agent: agent)) {
                            AgentRow(agent: agent)
                        }
                        .listRowBackground(Color.jCard)
                    }
                }
                .scrollContentBackground(.hidden)
                .refreshable { await load() }
            }
            .navigationTitle("")
            .navigationBarTitleDisplayMode(.inline)
            .toolbar {
                ToolbarItem(placement: .principal) {
                    Text("AGENTS")
                        .font(.system(size: 13, weight: .bold, design: .monospaced))
                        .tracking(6)
                        .foregroundColor(.jBlue)
                }
            }
            .toolbarBackground(Color.jCard, for: .navigationBar)
            .toolbarBackground(.visible, for: .navigationBar)
        }
        .task { await load() }
    }

    private func load() async {
        do {
            agents = try await JarvisClient.shared.fetchAgentFeed()
            errorText = nil
        } catch {
            errorText = "Couldn't reach Jarvis: \(error.localizedDescription)"
        }
    }
}

private struct AgentRow: View {
    let agent: JarvisClient.AgentFeedItem

    var body: some View {
        VStack(alignment: .leading, spacing: 4) {
            HStack {
                Text(agent.persona.isEmpty ? agent.displayName
                     : "\(agent.persona) · \(agent.displayName)")
                    .font(.system(size: 14, weight: .semibold))
                    .foregroundColor(.jText)
                Spacer()
                Circle()
                    .fill(agent.status == "running" ? Color.yellow
                          : agent.status == "error" ? Color.red : Color.jGreen)
                    .frame(width: 8, height: 8)
            }
            if !agent.lastRun.isEmpty {
                Text("last run \(String(agent.lastRun.prefix(16)).replacingOccurrences(of: "T", with: " "))")
                    .font(.system(size: 10, design: .monospaced))
                    .foregroundColor(.jBlueDim)
            }
            Text(agent.report.isEmpty ? "No report yet." : agent.report)
                .font(.system(size: 12))
                .foregroundColor(.jBlueDim)
                .lineLimit(2)
        }
        .padding(.vertical, 2)
    }
}

private struct AgentReportView: View {
    let agent: JarvisClient.AgentFeedItem

    var body: some View {
        ZStack {
            Color.jBg.ignoresSafeArea()
            ScrollView {
                VStack(alignment: .leading, spacing: 12) {
                    if !agent.description.isEmpty {
                        Text(agent.description)
                            .font(.system(size: 12, design: .monospaced))
                            .foregroundColor(.jBlueDim)
                    }
                    Text(agent.report.isEmpty ? "No report yet — this agent hasn't run."
                         : agent.report)
                        .font(.system(size: 14))
                        .foregroundColor(.jText)
                        .textSelection(.enabled)
                }
                .padding(16)
            }
        }
        .navigationTitle(agent.persona.isEmpty ? agent.displayName : agent.persona)
        .navigationBarTitleDisplayMode(.inline)
        .toolbarBackground(Color.jCard, for: .navigationBar)
    }
}
