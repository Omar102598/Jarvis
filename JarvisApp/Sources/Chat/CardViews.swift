import SwiftUI
import Charts

// MARK: - Rich chat cards
//
// Native renderings of the structured cards Jarvis's tools attach to a reply
// (see ChatCard in Models.swift and services/llm_agent/tools/cards.py). The
// data is computed server-side from real logs — these views only draw it.

struct ChatCardView: View {
    let card: ChatCard

    var body: some View {
        switch card.type {
        case "macros": MacroCardView(card: card)
        case "stats":  StatsCardView(card: card)
        case "table":  TableCardView(card: card)
        case "chart":  ChartCardView(card: card)
        case "action": ActionCardView(card: card)
        default:       EmptyView()   // a newer card type than this build knows
        }
    }
}

/// Shared chrome: a titled panel that sits inside the reply bubble.
private struct CardFrame<Content: View>: View {
    let title: String
    var subtitle: String = ""
    var trailing: String = ""
    @ViewBuilder let content: Content

    var body: some View {
        VStack(alignment: .leading, spacing: 10) {
            if !title.isEmpty || !trailing.isEmpty {
                HStack(alignment: .firstTextBaseline) {
                    Text(title)
                        .font(.system(size: 13, weight: .semibold))
                        .foregroundColor(.jText)
                    Spacer()
                    if !trailing.isEmpty {
                        Text(trailing)
                            .font(.system(size: 10, weight: .semibold))
                            .foregroundColor(.jGreen)
                            .padding(.horizontal, 7).padding(.vertical, 2)
                            .background(Color.jGreen.opacity(0.15))
                            .clipShape(Capsule())
                    }
                }
            }
            if !subtitle.isEmpty {
                Text(subtitle)
                    .font(.system(size: 11))
                    .foregroundColor(.jText.opacity(0.6))
                    .fixedSize(horizontal: false, vertical: true)
            }
            content
        }
        .padding(12)
        .frame(maxWidth: .infinity, alignment: .leading)
        .background(Color.jBg.opacity(0.7))
        .overlay(RoundedRectangle(cornerRadius: 10).stroke(Color.jBorder, lineWidth: 1))
        .cornerRadius(10)
    }
}

private func barColor(_ name: String) -> Color {
    switch name {
    case "gold":  return .jGold
    case "green": return .jGreen
    case "red":   return .red
    default:      return .jBlue
    }
}

private func fmt(_ v: Double) -> String {
    let n = NumberFormatter()
    n.numberStyle = .decimal
    n.maximumFractionDigits = v.rounded() == v ? 0 : 1
    return n.string(from: NSNumber(value: v)) ?? "\(v)"
}

// MARK: Macros — the daily nutrition tracker

struct MacroCardView: View {
    let card: ChatCard
    @State private var showMeals = false

    var body: some View {
        CardFrame(title: card.title) {
            if let cal = card.calories {
                VStack(alignment: .leading, spacing: 2) {
                    HStack(alignment: .firstTextBaseline, spacing: 4) {
                        Text(fmt(cal.value))
                            .font(.system(size: 30, weight: .bold, design: .rounded))
                            .foregroundColor(.jText)
                        if cal.target > 0 {
                            Text("/ \(fmt(cal.target))")
                                .font(.system(size: 18, weight: .semibold, design: .rounded))
                                .foregroundColor(.jText.opacity(0.5))
                        }
                    }
                    Text("Calories consumed")
                        .font(.system(size: 11))
                        .foregroundColor(.jText.opacity(0.55))
                }
            }
            ForEach(card.bars, id: \.label) { bar in
                MacroBarRow(bar: bar)
            }
            if !card.meals.isEmpty {
                Button {
                    withAnimation(.easeInOut(duration: 0.15)) { showMeals.toggle() }
                } label: {
                    Label(showMeals ? "Hide meals" : "\(card.meals.count) meal\(card.meals.count == 1 ? "" : "s") logged",
                          systemImage: showMeals ? "chevron.up" : "fork.knife")
                        .font(.system(size: 11, weight: .medium))
                        .foregroundColor(.jBlue)
                }
                if showMeals {
                    VStack(alignment: .leading, spacing: 4) {
                        ForEach(Array(card.meals.enumerated()), id: \.offset) { i, meal in
                            Text("\(i + 1). \(meal)")
                                .font(.system(size: 11))
                                .foregroundColor(.jText.opacity(0.75))
                        }
                    }
                }
            }
            if !card.footer.isEmpty {
                Divider().background(Color.jBorder)
                Text(card.footer)
                    .font(.system(size: 13, weight: .semibold))
                    .foregroundColor(.jText)
            }
        }
    }
}

private struct MacroBarRow: View {
    let bar: ChatCard.MacroBar

    private var fraction: Double {
        guard bar.target > 0 else { return 0 }
        return min(bar.value / bar.target, 1)
    }

    var body: some View {
        VStack(alignment: .leading, spacing: 4) {
            HStack {
                Text(bar.label)
                    .font(.system(size: 12, weight: .semibold))
                    .foregroundColor(.jText)
                Spacer()
                Text(bar.target > 0
                     ? "\(fmt(bar.value)) / \(fmt(bar.target))\(bar.unit)"
                     : "\(fmt(bar.value))\(bar.unit)")
                    .font(.system(size: 12, design: .rounded))
                    .foregroundColor(.jText.opacity(0.8))
            }
            GeometryReader { geo in
                ZStack(alignment: .leading) {
                    Capsule().fill(Color.jBorder)
                    Capsule().fill(barColor(bar.color))
                        .frame(width: max(geo.size.width * fraction, fraction > 0 ? 6 : 0))
                }
            }
            .frame(height: 7)
            if bar.target > 0 {
                let left = bar.target - bar.value
                Text(left >= 0 ? "\(fmt(left))\(bar.unit) remaining"
                               : "\(fmt(-left))\(bar.unit) over")
                    .font(.system(size: 10))
                    .foregroundColor(left >= 0 ? .jText.opacity(0.5) : .jGold)
            }
        }
    }
}

// MARK: Stats — a 2-column grid of labelled values

struct StatsCardView: View {
    let card: ChatCard
    private let cols = [GridItem(.flexible(), spacing: 10, alignment: .topLeading),
                        GridItem(.flexible(), spacing: 10, alignment: .topLeading)]

    var body: some View {
        CardFrame(title: card.title, subtitle: card.subtitle) {
            LazyVGrid(columns: cols, alignment: .leading, spacing: 12) {
                ForEach(Array(card.items.enumerated()), id: \.offset) { _, item in
                    VStack(alignment: .leading, spacing: 2) {
                        Text(item.label)
                            .font(.system(size: 10, weight: .medium))
                            .foregroundColor(.jText.opacity(0.55))
                        Text(item.value)
                            .font(.system(size: 18, weight: .bold, design: .rounded))
                            .foregroundColor(.jText)
                            .lineLimit(2)
                            .minimumScaleFactor(0.7)
                        if !item.sub.isEmpty {
                            Text(item.sub)
                                .font(.system(size: 10))
                                .foregroundColor(.jText.opacity(0.45))
                                .lineLimit(1)
                        }
                    }
                }
            }
            if !card.footer.isEmpty {
                Text(card.footer).font(.system(size: 11)).foregroundColor(.jText.opacity(0.6))
            }
        }
    }
}

// MARK: Table

struct TableCardView: View {
    let card: ChatCard

    private var visibleColumns: [Int] {
        // Drop columns that are empty in every row (e.g. an all-blank "Cue").
        card.columns.indices.filter { c in
            card.rows.contains { $0.indices.contains(c) && !$0[c].isEmpty }
        }
    }

    var body: some View {
        CardFrame(title: card.title) {
            let cols = visibleColumns
            VStack(alignment: .leading, spacing: 0) {
                HStack(alignment: .top, spacing: 8) {
                    ForEach(cols, id: \.self) { c in
                        Text(card.columns[c])
                            .font(.system(size: 10, weight: .semibold))
                            .foregroundColor(.jText.opacity(0.55))
                            .frame(maxWidth: .infinity, alignment: .leading)
                    }
                }
                .padding(.bottom, 6)
                ForEach(Array(card.rows.enumerated()), id: \.offset) { _, row in
                    Divider().background(Color.jBorder)
                    HStack(alignment: .top, spacing: 8) {
                        ForEach(cols, id: \.self) { c in
                            Text(row.indices.contains(c) ? row[c] : "")
                                .font(.system(size: 12, weight: c == cols.first ? .medium : .regular))
                                .foregroundColor(.jText.opacity(c == cols.first ? 1 : 0.8))
                                .frame(maxWidth: .infinity, alignment: .leading)
                                .fixedSize(horizontal: false, vertical: true)
                        }
                    }
                    .padding(.vertical, 7)
                }
            }
            if !card.footer.isEmpty {
                Text(card.footer).font(.system(size: 10)).foregroundColor(.jText.opacity(0.5))
            }
        }
    }
}

// MARK: Chart — calories by day (bar) or weight trend (line), with target rule

struct ChartCardView: View {
    let card: ChatCard

    var body: some View {
        CardFrame(title: card.title) {
            Chart {
                ForEach(card.points) { p in
                    if card.kind == "line" {
                        LineMark(x: .value("Day", p.x), y: .value(card.unit, p.y))
                            .foregroundStyle(Color.jBlue)
                            .interpolationMethod(.catmullRom)
                        PointMark(x: .value("Day", p.x), y: .value(card.unit, p.y))
                            .foregroundStyle(Color.jBlue)
                            .symbolSize(18)
                    } else {
                        BarMark(x: .value("Day", p.x), y: .value(card.unit, p.y))
                            .foregroundStyle(card.target > 0 && p.y > card.target * 1.05
                                             ? Color.jGold : Color.jBlue)
                            .cornerRadius(3)
                    }
                }
                if card.target > 0 {
                    RuleMark(y: .value("Target", card.target))
                        .foregroundStyle(Color.jGreen.opacity(0.8))
                        .lineStyle(StrokeStyle(lineWidth: 1, dash: [4, 3]))
                        .annotation(position: .top, alignment: .trailing) {
                            Text("target \(fmt(card.target))")
                                .font(.system(size: 9))
                                .foregroundColor(.jGreen)
                        }
                }
            }
            .chartYScale(domain: yDomain)
            .chartXAxis {
                AxisMarks { _ in
                    AxisValueLabel().font(.system(size: 9)).foregroundStyle(Color.jText.opacity(0.6))
                }
            }
            .chartYAxis {
                AxisMarks(position: .leading) { _ in
                    AxisGridLine().foregroundStyle(Color.jBorder)
                    AxisValueLabel().font(.system(size: 9)).foregroundStyle(Color.jText.opacity(0.6))
                }
            }
            .frame(height: 150)
            if !card.footer.isEmpty {
                Text(card.footer).font(.system(size: 10)).foregroundColor(.jText.opacity(0.55))
            }
        }
    }

    /// Bars start at zero; a weight line zooms to its range (±2) so a
    /// half-pound trend is actually visible instead of a flat line at 200.
    private var yDomain: ClosedRange<Double> {
        let ys = card.points.map(\.y) + (card.target > 0 ? [card.target] : [])
        guard let lo = ys.min(), let hi = ys.max() else { return 0...1 }
        if card.kind == "line" { return (lo - 2)...(hi + 2) }
        return 0...max(hi * 1.1, 1)
    }
}

// MARK: Action — "Tap to set up" (approval) or a confirmation

struct ActionCardView: View {
    let card: ChatCard
    @State private var state: String = ""      // "", "working", "approved", "denied", "error"

    private var resolved: String { state.isEmpty ? card.status : state }

    var body: some View {
        HStack(alignment: .center, spacing: 12) {
            Image(systemName: card.icon.isEmpty ? "sparkles" : card.icon)
                .font(.system(size: 20))
                .foregroundColor(.jBlue)
                .frame(width: 38, height: 38)
                .background(Color.jBlue.opacity(0.12))
                .cornerRadius(9)
            VStack(alignment: .leading, spacing: 2) {
                Text(card.title)
                    .font(.system(size: 13, weight: .semibold))
                    .foregroundColor(.jText)
                Text(statusLine)
                    .font(.system(size: 11))
                    .foregroundColor(statusColor)
            }
            Spacer()
            if resolved == "pending" && !card.approvalID.isEmpty {
                HStack(spacing: 6) {
                    Button { decide(false) } label: {
                        Image(systemName: "xmark")
                            .font(.system(size: 12, weight: .bold))
                            .frame(width: 30, height: 30)
                    }
                    .foregroundColor(.jText.opacity(0.6))
                    .background(Color.jBorder)
                    .clipShape(Circle())
                    Button { decide(true) } label: {
                        Text("Set up")
                            .font(.system(size: 12, weight: .semibold))
                            .padding(.horizontal, 12).frame(height: 30)
                    }
                    .foregroundColor(.black)
                    .background(Color.jBlue)
                    .clipShape(Capsule())
                }
            } else if resolved == "working" {
                ProgressView().tint(.jBlue)
            }
        }
        .padding(12)
        .background(Color.jBg.opacity(0.7))
        .overlay(RoundedRectangle(cornerRadius: 10).stroke(Color.jBorder, lineWidth: 1))
        .cornerRadius(10)
    }

    private var statusLine: String {
        switch resolved {
        case "approved", "done": return "\(card.subtitle) · Scheduled ✓"
        case "denied":           return "\(card.subtitle) · Dismissed"
        case "error":            return "Couldn't reach Jarvis — try again"
        default:                 return card.footer.isEmpty ? card.subtitle
                                                            : "\(card.subtitle) · \(card.footer)"
        }
    }

    private var statusColor: Color {
        switch resolved {
        case "approved", "done": return .jGreen
        case "error":            return .jGold
        default:                 return .jText.opacity(0.6)
        }
    }

    private func decide(_ approve: Bool) {
        state = "working"
        Task {
            do {
                try await JarvisClient.shared.resolveApproval(id: card.approvalID, approve: approve)
                await MainActor.run { state = approve ? "approved" : "denied" }
            } catch {
                await MainActor.run { state = "error" }
            }
        }
    }
}
