import Foundation
import CoreLocation
import NetworkExtension

/// Home presence — the reliable arrival signal Jarvis can't get from one indoor
/// camera. Uses THREE layers so a single failure (e.g. iOS terminating the app)
/// doesn't lose an arrival:
///   1. Region monitoring (CLCircularRegion) — entry/exit while backgrounded.
///   2. Significant-Location-Change — relaunches a *terminated* app far more
///      reliably than region monitoring, and self-heals presence from distance.
///   3. On launch/foreground: requestState → self-heal a MISSED arrival (the app
///      was killed while you were out, so no entry event fired on your return).
///   + Wi-Fi: joining your home network also counts as home.
/// Posts are de-duped (only on change). Set home once from Settings.
final class LocationManager: NSObject, ObservableObject, CLLocationManagerDelegate {
    static let shared = LocationManager()

    private let manager = CLLocationManager()
    private let regionId = "jarvis.home"
    private let radius: CLLocationDistance = 120   // meters — covers an apartment complex

    @Published var authorized = false
    @Published var homeSet = false
    @Published var status = "not set"

    /// Distinguishes the one-shot "set home" fix from ongoing SLC updates.
    private var isSettingHome = false

    override init() {
        super.init()
        manager.delegate = self
        manager.desiredAccuracy = kCLLocationAccuracyHundredMeters
        homeSet = homeCoordinate != nil
    }

    // MARK: Persisted state

    private var homeCoordinate: CLLocationCoordinate2D? {
        get {
            let d = UserDefaults.standard
            guard d.object(forKey: "homeLat") != nil else { return nil }
            return CLLocationCoordinate2D(latitude: d.double(forKey: "homeLat"),
                                          longitude: d.double(forKey: "homeLon"))
        }
        set {
            let d = UserDefaults.standard
            if let c = newValue {
                d.set(c.latitude, forKey: "homeLat")
                d.set(c.longitude, forKey: "homeLon")
            } else {
                d.removeObject(forKey: "homeLat")
                d.removeObject(forKey: "homeLon")
            }
        }
    }

    /// Home Wi-Fi SSID, captured when home is set (needs the "Access WiFi
    /// Information" capability to read; harmlessly nil without it).
    private var homeSSID: String? {
        get { UserDefaults.standard.string(forKey: "homeSSID") }
        set { UserDefaults.standard.set(newValue, forKey: "homeSSID") }
    }

    /// Last presence we posted ("arrived"/"left") — for de-dup + self-heal.
    private var lastPosted: String? {
        get { UserDefaults.standard.string(forKey: "lastPresence") }
        set { UserDefaults.standard.set(newValue, forKey: "lastPresence") }
    }

    // MARK: Public API

    /// Begin monitoring (call at launch and on foreground). Requests Always auth,
    /// starts all layers, and self-heals a missed arrival.
    func start() {
        manager.requestAlwaysAuthorization()
        if homeCoordinate != nil { beginMonitoring() }
        checkWiFiArrival()
    }

    /// Re-evaluate on app foreground — catches an arrival the geofence missed.
    func refreshOnForeground() {
        guard let home = homeCoordinate,
              CLLocationManager.isMonitoringAvailable(for: CLCircularRegion.self) else { return }
        let region = CLCircularRegion(center: home, radius: radius, identifier: regionId)
        manager.requestState(for: region)   // → didDetermineState → self-heal
        checkWiFiArrival()
    }

    /// Capture the phone's current spot (and Wi-Fi) as "home" and start monitoring.
    func setHomeToCurrentLocation() {
        status = "getting location…"
        isSettingHome = true
        manager.requestWhenInUseAuthorization()
        manager.requestLocation()
        currentSSID { [weak self] ssid in if let s = ssid { self?.homeSSID = s } }
    }

    private func beginMonitoring() {
        guard let home = homeCoordinate,
              CLLocationManager.isMonitoringAvailable(for: CLCircularRegion.self) else { return }
        manager.monitoredRegions.forEach { manager.stopMonitoring(for: $0) }
        let region = CLCircularRegion(center: home, radius: radius, identifier: regionId)
        region.notifyOnEntry = true
        region.notifyOnExit = true
        manager.startMonitoring(for: region)
        // Backstop: SLC relaunches a terminated app far more reliably than region
        // monitoring alone (which stops firing if the app is force-quit).
        manager.startMonitoringSignificantLocationChanges()
        manager.requestState(for: region)   // self-heal current state on start
        homeSet = true
        status = "monitoring home"
    }

    // MARK: CLLocationManagerDelegate

    func locationManagerDidChangeAuthorization(_ mgr: CLLocationManager) {
        let s = mgr.authorizationStatus
        authorized = (s == .authorizedAlways || s == .authorizedWhenInUse)
        if s == .authorizedAlways, homeCoordinate != nil { beginMonitoring() }
    }

    func locationManager(_ mgr: CLLocationManager, didUpdateLocations locs: [CLLocation]) {
        guard let loc = locs.last else { return }
        if isSettingHome {
            isSettingHome = false
            homeCoordinate = loc.coordinate
            status = "home set ✓"
            beginMonitoring()
            return
        }
        // An SLC update — self-heal presence from actual distance to home.
        evaluatePresence(from: loc)
    }

    func locationManager(_ mgr: CLLocationManager, didEnterRegion region: CLRegion) {
        guard region.identifier == regionId else { return }
        status = "home"
        post("arrived")
    }

    func locationManager(_ mgr: CLLocationManager, didExitRegion region: CLRegion) {
        guard region.identifier == regionId else { return }
        status = "away"
        post("left")
    }

    func locationManager(_ mgr: CLLocationManager, didDetermineState state: CLRegionState,
                         for region: CLRegion) {
        guard region.identifier == regionId else { return }
        // THE fix for a missed arrival: launched/woke inside home but we last
        // posted "left" → post "arrived" now.
        if state == .inside {
            status = "home"
            post("arrived")
        } else if state == .outside {
            status = "away"
            post("left")
        }
    }

    func locationManager(_ mgr: CLLocationManager, didFailWithError error: Error) {
        status = "location error"
    }

    // MARK: Self-heal helpers

    private func evaluatePresence(from loc: CLLocation) {
        guard let home = homeCoordinate else { return }
        let d = loc.distance(from: CLLocation(latitude: home.latitude, longitude: home.longitude))
        if d <= radius {
            status = "home"; post("arrived")
        } else {
            status = "away"; post("left")
        }
    }

    // MARK: Wi-Fi

    /// If we're on the saved home network, treat it as an arrival.
    func checkWiFiArrival() {
        guard let home = homeSSID else { return }
        currentSSID { [weak self] ssid in
            if let s = ssid, s == home {
                self?.status = "home (wifi)"
                self?.post("arrived")
            }
        }
    }

    /// Current Wi-Fi SSID (nil without the Access-WiFi-Information capability).
    private func currentSSID(_ completion: @escaping (String?) -> Void) {
        NEHotspotNetwork.fetchCurrent { network in
            DispatchQueue.main.async { completion(network?.ssid) }
        }
    }

    // MARK: Backend (de-duped — only post on a real state change)

    private func post(_ event: String) {
        guard lastPosted != event else { return }
        lastPosted = event
        Task { await postPresence(event) }
    }

    private func postPresence(_ event: String) async {
        guard let url = URL(string: "\(JarvisConfig.serverURL)/presence/location") else { return }
        var req = URLRequest(url: url)
        req.httpMethod = "POST"
        req.setValue("application/json", forHTTPHeaderField: "Content-Type")
        if !JarvisConfig.apiKey.isEmpty {
            req.setValue(JarvisConfig.apiKey, forHTTPHeaderField: "X-API-Key")
        }
        req.httpBody = try? JSONSerialization.data(withJSONObject: ["event": event])
        _ = try? await URLSession.shared.data(for: req)
    }
}
