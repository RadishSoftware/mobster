import SwiftUI
import UserNotifications

/// The daily reminder, its time, and a way back to the paywall.
struct SettingsView: View {
    @Environment(AppRouter.self) private var router
    @AppStorage(Keys.dailyReminder) private var savedReminder = false
    @AppStorage(Keys.reminderMinutes) private var reminderMinutes = 7 * 60 + 30
    /// Where the switch lands when the reminder-not-saved bug is planted: this screen's memory only.
    @State private var unsavedReminder: Bool?

    var body: some View {
        Form {
            Section {
                Toggle(isOn: reminderOn) {
                    SettingsLabel(title: "Daily reminder", symbol: "bell.fill", tint: Color(hex: 0xF0703F))
                }
                .accessibilityIdentifier("daily_reminder")
                DatePicker(selection: reminderTime, displayedComponents: .hourAndMinute) {
                    SettingsLabel(title: "Reminder time", symbol: "clock.fill", tint: Color(hex: 0x6A5BD6))
                }
                .disabled(!reminderOn.wrappedValue)
            } header: {
                Text("Reminders")
            } footer: {
                Text("Daybreak sends one notification a day at this time.")
            }
            .listRowBackground(Theme.surface)

            Section("Daybreak Plus") {
                Button {
                    router.showPaywall = true
                } label: {
                    HStack(spacing: 12) {
                        AppGlyph(size: 30)
                        Text("Show paywall")
                            .foregroundStyle(Theme.ink)
                        Spacer()
                        Image(systemName: "chevron.right")
                            .font(.footnote.weight(.semibold))
                            .foregroundStyle(Theme.tertiary)
                            .accessibilityHidden(true)
                    }
                }
                .accessibilityIdentifier("settings_paywall")
            }
            .listRowBackground(Theme.surface)

            Section("About") {
                LabeledContent {
                    Text(appVersion)
                } label: {
                    SettingsLabel(title: "Version", symbol: "info", tint: Color(hex: 0x8E8A96))
                }
                LabeledContent {
                    Text("On this iPhone only")
                } label: {
                    SettingsLabel(title: "Data", symbol: "iphone", tint: Color(hex: 0x8E8A96))
                }
            }
            .listRowBackground(Theme.surface)
        }
        .scrollContentBackground(.hidden)
        .background(Theme.background)
        .navigationTitle("Settings")
        .navigationBarTitleDisplayMode(.large)
        .onChange(of: reminderOn.wrappedValue) { _, isOn in
            ReminderScheduler.update(isOn: isOn, minutes: reminderMinutes)
        }
        .onChange(of: reminderMinutes) { _, minutes in
            ReminderScheduler.update(isOn: reminderOn.wrappedValue, minutes: minutes)
        }
    }

    private var reminderOn: Binding<Bool> {
        Binding(
            get: { unsavedReminder ?? savedReminder },
            set: { isOn in
                if Bug.reminderNotSaved.isOn {
                    unsavedReminder = isOn
                } else {
                    savedReminder = isOn
                }
            }
        )
    }

    private var reminderTime: Binding<Date> {
        Binding(
            get: {
                Calendar.current.date(bySettingHour: reminderMinutes / 60, minute: reminderMinutes % 60,
                                      second: 0, of: .now) ?? .now
            },
            set: { date in
                let parts = Calendar.current.dateComponents([.hour, .minute], from: date)
                reminderMinutes = (parts.hour ?? 7) * 60 + (parts.minute ?? 30)
            }
        )
    }

    private var appVersion: String {
        let info = Bundle.main.infoDictionary
        let version = info?["CFBundleShortVersionString"] as? String ?? "1.0"
        let build = info?["CFBundleVersion"] as? String ?? "1"
        return "\(version) (\(build))"
    }
}

/// A settings row's title beside a white symbol on a coloured tile, as in the Settings app.
private struct SettingsLabel: View {
    let title: String
    let symbol: String
    let tint: Color

    var body: some View {
        Label {
            Text(title)
                .foregroundStyle(Theme.ink)
        } icon: {
            Image(systemName: symbol)
                .font(.system(size: 14, weight: .semibold))
                .foregroundStyle(.white)
                .frame(width: 30, height: 30)
                .background(RoundedRectangle(cornerRadius: 7, style: .continuous).fill(tint))
                .accessibilityHidden(true)
        }
    }
}

/// Schedules the one daily local notification, when notifications are allowed. Nothing leaves the phone.
enum ReminderScheduler {
    static let identifier = "daybreak.daily"

    static func update(isOn: Bool, minutes: Int) {
        let center = UNUserNotificationCenter.current()
        center.removePendingNotificationRequests(withIdentifiers: [identifier])
        guard isOn else { return }
        Task {
            guard await center.notificationSettings().authorizationStatus == .authorized else { return }
            let content = UNMutableNotificationContent()
            content.title = "Good morning"
            content.body = "Three habits today. Start with a glass of water."
            let trigger = UNCalendarNotificationTrigger(
                dateMatching: DateComponents(hour: minutes / 60, minute: minutes % 60), repeats: true)
            try? await center.add(UNNotificationRequest(identifier: identifier, content: content, trigger: trigger))
        }
    }
}
