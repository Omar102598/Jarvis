import Foundation
import CoreLocation

/// Home geofence — the reliable arrival signal Jarvis can't get from one indoor
/// camera. Monitors a region around home (works in the background, even if the
/// app is killed) and tells the backend when you arrive or leave. Set your home
/// once with `setHomeToCurrentLocation()` from Settings.
final class LocationManager: NSObject, ObservableObject, CLLocationManagerDelegate {
    static let shared = LocationManager()

    private let manager = CLLocationManager()
    private let regionId = "jarvis.home"
    private let radius: CLLocationDistance = 120   // meters — covers an apartment complex

    @Published var authorized = false
    @Published var homeSet = false
    @Published var status = "not set"

    override init() {
        super.init()
        manager.delegate = self
        manager.desiredAccuracy = kCLLocationAccuracyHundredMeters
        homeSet = homeCoordinate != nil
    }

    // MARK: Persisted home coordinate

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

    // MARK: Public API

    /// Ask for Always authorization (required for background geofencing) and
    /// begin monitoring if home is already set.
    func start() {
        manager.requestAlwaysAuthorization()
        if homeCoordinate != nil { beginMonitoring() }
    }

    /// Capture the phone's current spot as "home" and start monitoring it.
    func setHomeToCurrentLocation() {
        status = "getting location…"
        manager.requestWhenInUseAuthorization()
        manager.requestLocation()   // one-shot fix → didUpdateLocations
    }

    private func beginMonitoring() {
        guard let home = homeCoordinate,
              CLLocationManager.isMonitoringAvailable(for: CLCircularRegion.self) else { return }
        manager.monitoredRegions.forEach { manager.stopMonitoring(for: $0) }
        let region = CLCircularRegion(center: home, radius: radius, identifier: regionId)
        region.notifyOnEntry = true
        region.notifyOnExit = true
        manager.startMonitoring(for: region)
        // Also evaluate current state so an app-launch-while-home is known.
        manager.requestState(for: region)
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
        homeCoordinate = loc.coordinate
        status = "home set ✓"
        beginMonitoring()
    }

    func locationManager(_ mgr: CLLocationManager, didEnterRegion region: CLRegion) {
        guard region.identifier == regionId else { return }
        status = "home"
        Task { await postPresence("arrived") }
    }

    func locationManager(_ mgr: CLLocationManager, didExitRegion region: CLRegion) {
        guard region.identifier == regionId else { return }
        status = "away"
        Task { await postPresence("left") }
    }

    func locationManager(_ mgr: CLLocationManager, didDetermineState state: CLRegionState,
                         for region: CLRegion) {
        guard region.identifier == regionId else { return }
        status = state == .inside ? "home" : "away"
    }

    func locationManager(_ mgr: CLLocationManager, didFailWithError error: Error) {
        status = "location error"
    }

    // MARK: Backend

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
