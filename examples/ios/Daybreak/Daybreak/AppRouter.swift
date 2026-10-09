import Foundation
import Observation

/// Where the app is, and the deep links that move it: daybreak://paywall, daybreak://settings, daybreak://today.
@MainActor
@Observable
final class AppRouter {
    enum Route: Hashable {
        case settings
    }

    private(set) var hasOnboarded: Bool
    var path: [Route] = []
    var showPaywall = false

    init(defaults: UserDefaults = .standard) {
        // `-DaybreakSkipOnboarding YES` lands in UserDefaults' argument domain for this launch only.
        hasOnboarded = defaults.bool(forKey: Keys.onboarded) || defaults.bool(forKey: "DaybreakSkipOnboarding")
    }

    /// The last onboarding page's "Get started": Today underneath, the paywall on top.
    func finishOnboarding() {
        markOnboarded()
        showPaywall = true
    }

    func open(_ url: URL) {
        guard url.scheme == "daybreak" else { return }
        switch url.host() {
        case "paywall":
            showPaywall = true
        case "settings":
            markOnboarded()
            showPaywall = false
            path = [.settings]
        case "today":
            markOnboarded()
            showPaywall = false
            path = []
        default:
            break
        }
    }

    private func markOnboarded() {
        hasOnboarded = true
        UserDefaults.standard.set(true, forKey: Keys.onboarded)
    }
}

enum Keys {
    static let onboarded = "onboarded"
    static let dailyReminder = "dailyReminder"
    static let reminderMinutes = "reminderMinutes"
}

/// Bugs planted for Mobster's docs and tests, switched on per launch with `-DaybreakBug <name>`.
/// Without the argument the app behaves correctly.
enum Bug: String, CaseIterable {
    /// The paywall shows two plans instead of three.
    case missingPlan = "missing-plan"
    /// The annual plan reads "$39.99 / month".
    case annualPrice = "annual-price"
    /// The daily reminder switch isn't saved, so it is off again after a relaunch.
    case reminderNotSaved = "reminder-not-saved"
    /// The paywall never finishes loading its plans.
    case stuckLoading = "stuck-loading"

    static var active: Bug? {
        UserDefaults.standard.string(forKey: "DaybreakBug").flatMap(Bug.init(rawValue:))
    }

    var isOn: Bool { Bug.active == self }
}
