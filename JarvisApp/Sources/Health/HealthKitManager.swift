import Foundation
import HealthKit

/// Reads a daily fitness snapshot from HealthKit and pushes it to the Jarvis
/// backend, where the grocery agent uses it to refine TDEE/macro targets and
/// the ambient agent watches it for anomalies (sleep drop, resting-HR spike).
///
/// Read-only: Jarvis never writes to HealthKit. Authorization is requested once
/// on first launch; the user can revoke it in Settings → Privacy → Health.
@MainActor
final class HealthKitManager: ObservableObject {
    static let shared = HealthKitManager()

    private let store = HKHealthStore()

    @Published private(set) var authorized = false
    @Published private(set) var lastSyncDescription = "Not yet synced"

    private init() {}

    // MARK: - Types we read

    private var readTypes: Set<HKObjectType> {
        var types = Set<HKObjectType>()
        if let steps = HKQuantityType.quantityType(forIdentifier: .stepCount) { types.insert(steps) }
        if let energy = HKQuantityType.quantityType(forIdentifier: .activeEnergyBurned) { types.insert(energy) }
        if let rhr = HKQuantityType.quantityType(forIdentifier: .restingHeartRate) { types.insert(rhr) }
        if let hrv = HKQuantityType.quantityType(forIdentifier: .heartRateVariabilitySDNN) { types.insert(hrv) }
        if let mass = HKQuantityType.quantityType(forIdentifier: .bodyMass) { types.insert(mass) }
        if let sleep = HKObjectType.categoryType(forIdentifier: .sleepAnalysis) { types.insert(sleep) }
        types.insert(HKObjectType.workoutType())
        return types
    }

    // MARK: - Authorization

    func requestAuthorization() async {
        guard HKHealthStore.isHealthDataAvailable() else {
            lastSyncDescription = "Health data unavailable on this device"
            return
        }
        do {
            try await store.requestAuthorization(toShare: [], read: readTypes)
            authorized = true
        } catch {
            print("[HealthKit] Authorization failed: \(error)")
            authorized = false
        }
    }

    /// Request authorization (if needed) and push a fresh snapshot. Safe to call
    /// on every app foreground — HealthKit only prompts once.
    func syncOnLaunch() async {
        guard HKHealthStore.isHealthDataAvailable() else { return }
        if !authorized { await requestAuthorization() }
        guard authorized else { return }
        await pushSnapshot()
    }

    // MARK: - Snapshot

    func pushSnapshot() async {
        let snapshot = await readSnapshot()
        do {
            let ok = try await JarvisClient.shared.pushHealthSnapshot(snapshot)
            lastSyncDescription = ok
                ? "Synced \(Self.shortTime(Date()))"
                : "Sync failed (server)"
        } catch {
            lastSyncDescription = "Sync error: \(error.localizedDescription)"
            print("[HealthKit] Push failed: \(error)")
        }
    }

    private func readSnapshot() async -> HealthSnapshot {
        async let steps = sumToday(.stepCount, unit: .count())
        async let energy = sumToday(.activeEnergyBurned, unit: .kilocalorie())
        async let rhr = mostRecent(.restingHeartRate, unit: HKUnit.count().unitDivided(by: .minute()))
        async let hrv = mostRecent(.heartRateVariabilitySDNN, unit: .secondUnit(with: .milli))
        async let massKg = mostRecent(.bodyMass, unit: .gramUnit(with: .kilo))
        async let sleep = sleepHoursLastNight()
        async let workouts = workoutsToday()

        let (w, m) = await workouts
        let massLbs = await massKg.map { $0 * 2.20462 }

        return HealthSnapshot(
            steps: await steps,
            activeEnergyKcal: await energy,
            restingHeartRate: await rhr,
            hrvMs: await hrv,
            sleepHours: await sleep,
            bodyMassLbs: massLbs,
            workoutsToday: w,
            workoutMinutesToday: m
        )
    }

    // MARK: - Query helpers

    private func sumToday(_ id: HKQuantityTypeIdentifier, unit: HKUnit) async -> Double? {
        guard let type = HKQuantityType.quantityType(forIdentifier: id) else { return nil }
        let predicate = HKQuery.predicateForSamples(
            withStart: Calendar.current.startOfDay(for: Date()),
            end: Date(),
            options: .strictStartDate
        )
        return await withCheckedContinuation { continuation in
            let q = HKStatisticsQuery(
                quantityType: type, quantitySamplePredicate: predicate, options: .cumulativeSum
            ) { _, stats, _ in
                continuation.resume(returning: stats?.sumQuantity()?.doubleValue(for: unit))
            }
            store.execute(q)
        }
    }

    private func mostRecent(_ id: HKQuantityTypeIdentifier, unit: HKUnit) async -> Double? {
        guard let type = HKQuantityType.quantityType(forIdentifier: id) else { return nil }
        let sort = NSSortDescriptor(key: HKSampleSortIdentifierEndDate, ascending: false)
        return await withCheckedContinuation { continuation in
            let q = HKSampleQuery(
                sampleType: type, predicate: nil, limit: 1, sortDescriptors: [sort]
            ) { _, samples, _ in
                let value = (samples?.first as? HKQuantitySample)?.quantity.doubleValue(for: unit)
                continuation.resume(returning: value)
            }
            store.execute(q)
        }
    }

    /// Total asleep hours from the most recent night (samples in the last 18h).
    private func sleepHoursLastNight() async -> Double? {
        guard let type = HKObjectType.categoryType(forIdentifier: .sleepAnalysis) else { return nil }
        let start = Date().addingTimeInterval(-18 * 3600)
        let predicate = HKQuery.predicateForSamples(withStart: start, end: Date(), options: [])
        return await withCheckedContinuation { continuation in
            let q = HKSampleQuery(
                sampleType: type, predicate: predicate, limit: HKObjectQueryNoLimit, sortDescriptors: nil
            ) { _, samples, _ in
                guard let samples = samples as? [HKCategorySample] else {
                    continuation.resume(returning: nil)
                    return
                }
                let asleepValues: Set<Int> = [
                    HKCategoryValueSleepAnalysis.asleepUnspecified.rawValue,
                    HKCategoryValueSleepAnalysis.asleepCore.rawValue,
                    HKCategoryValueSleepAnalysis.asleepDeep.rawValue,
                    HKCategoryValueSleepAnalysis.asleepREM.rawValue,
                ]
                let seconds = samples
                    .filter { asleepValues.contains($0.value) }
                    .reduce(0.0) { $0 + $1.endDate.timeIntervalSince($1.startDate) }
                continuation.resume(returning: seconds > 0 ? seconds / 3600 : nil)
            }
            store.execute(q)
        }
    }

    private func workoutsToday() async -> (Int?, Double?) {
        let predicate = HKQuery.predicateForSamples(
            withStart: Calendar.current.startOfDay(for: Date()),
            end: Date(),
            options: .strictStartDate
        )
        return await withCheckedContinuation { continuation in
            let q = HKSampleQuery(
                sampleType: HKObjectType.workoutType(),
                predicate: predicate, limit: HKObjectQueryNoLimit, sortDescriptors: nil
            ) { _, samples, _ in
                guard let workouts = samples as? [HKWorkout], !workouts.isEmpty else {
                    continuation.resume(returning: (nil, nil))
                    return
                }
                let minutes = workouts.reduce(0.0) { $0 + $1.duration } / 60
                continuation.resume(returning: (workouts.count, minutes))
            }
            store.execute(q)
        }
    }

    private static func shortTime(_ date: Date) -> String {
        let f = DateFormatter()
        f.dateFormat = "HH:mm"
        return f.string(from: date)
    }
}
