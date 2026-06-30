// HUDRenderer maps HUDState values to MWDATDisplay component trees and sends them.
// This file intentionally does NOT import SwiftUI to avoid Text/Button name collisions.

import Foundation
import MWDATDisplay

// MARK: - HUD states

enum HUDState {
    case idle
    case listening
    case thinking
    case text(title: String, body: String)
    case image(urlString: String)
    case video(urlString: String)
    case error(message: String)
}

// MARK: - HUDRenderer

struct HUDRenderer {

    func send(_ state: HUDState, to display: Display) async throws {
        switch state {
        case .idle:
            try await sendIdle(to: display)

        case .listening:
            try await display.send(
                FlexBox(direction: .column, spacing: 8) {
                    Text("Listening...", style: .heading)
                    Text("Speak your request", style: .body)
                }
                .padding(20)
                .background(.card)
            )

        case .thinking:
            try await display.send(
                FlexBox(direction: .column, spacing: 8) {
                    Text("Thinking...", style: .heading)
                }
                .padding(20)
                .background(.card)
            )

        case .text(let title, let body):
            try await display.send(
                FlexBox(direction: .column, spacing: 12) {
                    Text(title, style: .heading)
                    Text(body, style: .body)
                    Button(label: "Ask Again", style: .secondary, onClick: {
                        NotificationCenter.default.post(name: .jarvisActivateWake, object: nil)
                    })
                }
                .padding(20)
                .background(.card)
            )

        case .image(let urlString):
            try await display.send(
                FlexBox(direction: .column, spacing: 12) {
                    Image(uri: urlString, sizePreset: .fill, cornerRadius: .medium)
                    Button(label: "Back", style: .secondary, onClick: {
                        NotificationCenter.default.post(name: .jarvisActivateWake, object: nil)
                    })
                }
                .padding(20)
                .background(.card)
            )

        case .video(let urlString):
            // Video root replaces the entire display; stop button not available mid-play
            try await display.send(
                VideoPlayer(provider: .uri(urlString), codec: .mp4, onError: { error in
                    print("[HUDRenderer] Video error: \(error)")
                    NotificationCenter.default.post(name: .jarvisActivateWake, object: nil)
                })
            )

        case .error(let message):
            try await display.send(
                FlexBox(direction: .column, spacing: 8) {
                    Text("Oops", style: .heading)
                    Text(message, style: .body)
                    Button(label: "Retry", style: .primary, onClick: {
                        NotificationCenter.default.post(name: .jarvisActivateWake, object: nil)
                    })
                }
                .padding(20)
                .background(.card)
            )
        }
    }

    // Idle card: permanent "Ask Jarvis" button — Neural Band / frame-touch activates it
    func sendIdle(to display: Display) async throws {
        try await display.send(
            FlexBox(direction: .column, spacing: 16) {
                Text("Jarvis", style: .heading)
                Button(label: "Ask Jarvis", style: .primary, iconName: .speechBubble, onClick: {
                    NotificationCenter.default.post(name: .jarvisActivateWake, object: nil)
                })
            }
            .padding(24)
            .background(.card)
        )
    }
}
