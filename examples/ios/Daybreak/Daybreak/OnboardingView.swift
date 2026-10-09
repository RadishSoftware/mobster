import SwiftUI
import UserNotifications

/// Three pages on the first launch, and the sun comes up a little on each. The last page asks for
/// notifications, then "Get started" opens the paywall.
struct OnboardingView: View {
    @Environment(AppRouter.self) private var router
    @State private var page = 0
    @State private var reminders: UNAuthorizationStatus = .notDetermined

    private let pages: [OnboardingPage] = [
        .init(title: "Small habits, every morning",
              body: "Pick a few things to do before the day gets busy. Daybreak keeps the list short and your progress in view.",
              sunrise: 0),
        .init(title: "Watch your streak grow",
              body: "Every morning you finish adds a day to your streak. The days that slip show up too.",
              sunrise: 0.42),
        .init(title: "One reminder a day",
              body: "A single nudge at the time you choose. Nothing else.",
              sunrise: 0.85),
    ]

    var body: some View {
        ZStack(alignment: .top) {
            Theme.background.ignoresSafeArea()
            SkyScene(sunrise: pages[page].sunrise, ground: 0.93)
                .frame(height: 510)
                .frame(maxHeight: .infinity, alignment: .top)
                .ignoresSafeArea(edges: .top)
            VStack(spacing: 0) {
                illustration
                    .frame(height: 300)
                    .padding(.top, 44)
                    .id(page)
                    .transition(.asymmetric(insertion: .offset(x: 40).combined(with: .opacity),
                                            removal: .offset(x: -40).combined(with: .opacity)))
                Spacer(minLength: 20)
                VStack(spacing: 12) {
                    Text(pages[page].title)
                        .font(.title.weight(.bold))
                        .foregroundStyle(Theme.ink)
                        .multilineTextAlignment(.center)
                        .accessibilityAddTraits(.isHeader)
                    Text(pages[page].body)
                        .font(.body)
                        .foregroundStyle(Theme.secondary)
                        .multilineTextAlignment(.center)
                        .fixedSize(horizontal: false, vertical: true)
                }
                .padding(.horizontal, 32)
                .id("text-\(page)")
                .transition(.opacity)
                Spacer(minLength: 20)
                PageDots(count: pages.count, current: page)
                    .padding(.bottom, 22)
                VStack(spacing: 12) {
                    if page == pages.count - 1 {
                        remindersButton
                        Button("Get started") { router.finishOnboarding() }
                            .buttonStyle(PrimaryButtonStyle())
                            .accessibilityIdentifier("onboarding_done")
                    } else {
                        Button("Continue") {
                            withAnimation(.spring(response: 0.7, dampingFraction: 1)) { page += 1 }
                        }
                        .buttonStyle(PrimaryButtonStyle())
                        .accessibilityIdentifier("onboarding_continue")
                    }
                }
                .padding(.horizontal, 24)
                .padding(.bottom, 8)
            }
        }
        .task { reminders = await UNUserNotificationCenter.current().notificationSettings().authorizationStatus }
    }

    @ViewBuilder
    private var illustration: some View {
        switch page {
        case 0: HabitListArt()
        case 1: StreakArt()
        default: ReminderArt()
        }
    }

    private var remindersButton: some View {
        Button(action: askForReminders) {
            Label(remindersTitle, systemImage: reminders == .authorized ? "checkmark" : "bell.fill")
        }
        .buttonStyle(SecondaryButtonStyle())
        .disabled(reminders != .notDetermined)
        .accessibilityIdentifier("onboarding_reminders")
    }

    private var remindersTitle: String {
        switch reminders {
        case .notDetermined: "Turn on reminders"
        case .denied: "Reminders are off"
        default: "Reminders are on"
        }
    }

    private func askForReminders() {
        Task {
            let center = UNUserNotificationCenter.current()
            _ = try? await center.requestAuthorization(options: [.alert, .sound, .badge])
            reminders = await center.notificationSettings().authorizationStatus
        }
    }
}

private struct OnboardingPage {
    let title: String
    let body: String
    /// How far the sun is up behind this page.
    let sunrise: Double
}

private struct PageDots: View {
    let count: Int
    let current: Int

    var body: some View {
        HStack(spacing: 6) {
            ForEach(0..<count, id: \.self) { index in
                Capsule()
                    .fill(index == current ? Theme.ink : Theme.hairline)
                    .frame(width: index == current ? 20 : 7, height: 7)
            }
        }
        .animation(.spring(response: 0.4, dampingFraction: 1), value: current)
        .accessibilityElement(children: .ignore)
        .accessibilityLabel("Page \(current + 1) of \(count)")
    }
}

// MARK: - Illustrations (decorative)

private struct HabitListArt: View {
    private let rows: [(Habit, Bool)] = [(Habit.water, true), (Habit.read, true), (Habit.walk, false)]

    var body: some View {
        VStack(spacing: 0) {
            ForEach(Array(rows.enumerated()), id: \.offset) { index, row in
                HStack(spacing: 12) {
                    HabitIcon(habit: row.0, size: 36)
                    Text(row.0.title)
                        .font(.subheadline.weight(.semibold))
                        .foregroundStyle(Theme.ink)
                    Spacer()
                    CheckCircle(isOn: row.1, size: 24)
                }
                .padding(.horizontal, 16)
                .frame(height: 60)
                if index < rows.count - 1 {
                    Rectangle().fill(Theme.hairline).frame(height: 0.5).padding(.leading, 64)
                }
            }
        }
        .surface(cornerRadius: 22)
        .frame(width: 300)
        .frame(maxHeight: .infinity)
        .accessibilityHidden(true)
    }
}

private struct StreakArt: View {
    // Five weeks of fictional history, oldest first: 1 done, 0 missed, -1 still ahead.
    // The last miss is twelve done days back, matching the 12-day streak.
    private let weeks: [[Int]] = [
        [1, 1, 0, 1, 1, 1, 1],
        [1, 1, 1, 1, 1, 0, 1],
        [1, 1, 1, 1, 0, 1, 1],
        [1, 1, 1, 1, 1, 1, 1],
        [1, 1, 1, -1, -1, -1, -1],
    ]

    var body: some View {
        VStack(alignment: .leading, spacing: 16) {
            HStack(alignment: .firstTextBaseline, spacing: 6) {
                Text("12")
                    .font(.system(size: 44, weight: .bold))
                    .foregroundStyle(Theme.ink)
                Text("day streak")
                    .font(.headline)
                    .foregroundStyle(Theme.secondary)
                Spacer()
                Image(systemName: "flame.fill")
                    .font(.title3)
                    .foregroundStyle(Theme.accent)
            }
            Grid(horizontalSpacing: 9, verticalSpacing: 9) {
                ForEach(weeks.indices, id: \.self) { week in
                    GridRow {
                        ForEach(weeks[week].indices, id: \.self) { day in
                            Circle()
                                .fill(color(for: weeks[week][day]))
                                .frame(width: 26, height: 26)
                        }
                    }
                }
            }
        }
        .padding(20)
        .frame(width: 300)
        .surface(cornerRadius: 26)
        .frame(maxHeight: .infinity)
        .accessibilityHidden(true)
    }

    private func color(for state: Int) -> Color {
        switch state {
        case 1: Theme.accent
        case 0: Theme.ink.opacity(0.10)
        default: Theme.ink.opacity(0.04)
        }
    }
}

private struct ReminderArt: View {
    @Environment(\.colorScheme) private var scheme

    var body: some View {
        VStack(spacing: 18) {
            Text("7:30")
                .font(.system(size: 84, weight: .semibold))
                .foregroundStyle(scheme == .dark ? Color.white : Theme.ink.opacity(0.88))
            HStack(alignment: .top, spacing: 12) {
                AppGlyph(size: 38)
                VStack(alignment: .leading, spacing: 2) {
                    HStack {
                        Text("Daybreak")
                            .font(.subheadline.weight(.semibold))
                        Spacer()
                        Text("now")
                            .font(.footnote)
                            .foregroundStyle(Theme.secondary)
                    }
                    Text("Good morning")
                        .font(.subheadline.weight(.semibold))
                    Text("Three habits today. Start with a glass of water.")
                        .font(.subheadline)
                        .foregroundStyle(Theme.secondary)
                }
                .foregroundStyle(Theme.ink)
            }
            .padding(14)
            .glass(in: RoundedRectangle(cornerRadius: 24, style: .continuous))
            .padding(.horizontal, 28)
        }
        .frame(maxHeight: .infinity)
        .accessibilityHidden(true)
    }
}

/// Daybreak's icon in miniature: the sun on the horizon, between two ridges.
struct AppGlyph: View {
    var size: CGFloat = 38

    private static let far: [CGPoint] = [CGPoint(x: -0.05, y: -0.02), CGPoint(x: 0.14, y: -0.07),
                                         CGPoint(x: 0.34, y: -0.035), CGPoint(x: 0.55, y: -0.085),
                                         CGPoint(x: 0.78, y: -0.04), CGPoint(x: 1.05, y: -0.075)]
    private static let near: [CGPoint] = [CGPoint(x: -0.05, y: 0.02), CGPoint(x: 0.22, y: -0.02),
                                          CGPoint(x: 0.48, y: 0.025), CGPoint(x: 0.74, y: -0.015),
                                          CGPoint(x: 1.05, y: 0.02)]

    var body: some View {
        ZStack {
            LinearGradient(stops: [.init(color: Color(hex: 0x14123A), location: 0),
                                   .init(color: Color(hex: 0x2E2463), location: 0.3),
                                   .init(color: Color(hex: 0x6A3576), location: 0.5),
                                   .init(color: Color(hex: 0xD4697A), location: 0.64),
                                   .init(color: Color(hex: 0xFF9A6A), location: 0.72)],
                           startPoint: .top, endPoint: .bottom)
            Circle()
                .fill(LinearGradient(colors: [Color(hex: 0xFFF5E2), Color(hex: 0xFFB07A)],
                                     startPoint: .top, endPoint: .bottom))
                .frame(width: size * 0.4, height: size * 0.4)
                .position(x: size * 0.5, y: size * 0.6)
            Ridge(points: Self.far, ground: size * 0.7, width: size).fill(Color(hex: 0x4C2C63))
            Ridge(points: Self.near, ground: size * 0.8, width: size).fill(Color(hex: 0x241A38))
        }
        .frame(width: size, height: size)
        .clipShape(RoundedRectangle(cornerRadius: size * 0.225, style: .continuous))
        .accessibilityHidden(true)
    }
}
