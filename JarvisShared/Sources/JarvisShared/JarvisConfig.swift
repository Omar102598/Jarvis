import Foundation

public struct JarvisConfig {
    public static var serverURL: String {
        get { store.string(forKey: "serverURL") ?? "http://192.168.1.100:8080" }
        set { store.set(newValue, forKey: "serverURL") }
    }
    public static var apiKey: String {
        get { store.string(forKey: "apiKey") ?? "" }
        set { store.set(newValue, forKey: "apiKey") }
    }

    #if os(macOS)
    public static var macBridgeURL: String {
        get { UserDefaults.standard.string(forKey: "macBridgeURL") ?? "http://localhost:7777" }
        set { UserDefaults.standard.set(newValue, forKey: "macBridgeURL") }
    }
    public static var repoPath: String {
        get { UserDefaults.standard.string(forKey: "repoPath") ?? "" }
        set { UserDefaults.standard.set(newValue, forKey: "repoPath") }
    }
    #endif

    private static let store = UserDefaults(suiteName: "group.com.jarvis.app") ?? .standard
}
