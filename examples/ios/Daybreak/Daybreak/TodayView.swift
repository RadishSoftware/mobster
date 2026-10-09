import SwiftUI

struct Habit: Identifiable {
    /// Also the habit switch's accessibility identifier.
    let id: String
    let title: String
    let detail: String
    let symbol: String
    let tint: Color

    static let water = Habit(id: "habit_water", title: "Drink water", detail: "8 glasses", symbol: "drop.fill",
                             tint: Color(light: 0x3E7FE0, dark: 0x6FA6FF))
    static let read = Habit(id: "habit_read", title: "Read 10 pages", detail: "Before bed", symbol: "book.fill",
                            tint: Color(light: 0xC27C12, dark: 0xF2B04E))
    static let walk = Habit(id: "habit_walk", title: "Walk 5,000 steps", detail: "Anytime today",
                            symbol: "figure.walk", tint: Color(light: 0x2F8F5B, dark: 0x5CC487))
}

/// Today's three habits, the streak and the week so far. The sun in the streak card climbs as habits get done.
struct TodayView: View {
    @Environment(AppRouter.self) private var router
    @AppStorage("habit_water") private var water = false
    @AppStorage("habit_read") private var read = false
    @AppStorage("habit_walk") private var walk = false

    /// Fictional history: twelve days in a row before today.
    private let streakBeforeToday = 12

    private let habits: [Habit] = [.water, .read, .walk]

    private var doneCount: Int { [water, read, walk].filter { $0 }.count }
    private var streak: Int { streakBeforeToday + (doneCount == habits.count ? 1 : 0) }
    private var progress: Double { Double(doneCount) / Double(habits.count) }

    var body: some View {
        ScrollView {
            VStack(alignment: .leading, spacing: 24) {
                header
                streakCard
                VStack(alignment: .leading, spacing: 12) {
                    HStack(alignment: .firstTextBaseline) {
                        Text("Habits")
                            .font(.title3.weight(.bold))
                            .accessibilityAddTraits(.isHeader)
                        Spacer()
                        Text("\(doneCount) of \(habits.count) done")
                            .font(.subheadline)
                            .foregroundStyle(Theme.secondary)
                            .contentTransition(.numericText())
                    }
                    VStack(spacing: 0) {
                        ForEach(habits) { habit in
                            HabitRow(habit: habit, isOn: binding(for: habit.id))
                            if habit.id != habits.last?.id {
                                Rectangle().fill(Theme.hairline).frame(height: 0.5).padding(.leading, 74)
                            }
                        }
                    }
                    .surface(cornerRadius: 22)
                }
            }
            .foregroundStyle(Theme.ink)
            .padding(.horizontal, 20)
            .padding(.bottom, 32)
        }
        .scrollBounceBehavior(.basedOnSize)
        .scrollIndicators(.hidden)
        .background(Theme.background)
        .toolbar(.hidden, for: .navigationBar)
        .sensoryFeedback(.success, trigger: doneCount == habits.count) { _, allDone in allDone }
    }

    private var header: some View {
        HStack(alignment: .center) {
            VStack(alignment: .leading, spacing: 2) {
                Text(Date.now.formatted(.dateTime.weekday(.wide).month(.wide).day()))
                    .font(.subheadline.weight(.semibold))
                    .foregroundStyle(Theme.secondary)
                Text("Today")
                    .font(.largeTitle.weight(.bold))
                    .accessibilityAddTraits(.isHeader)
            }
            Spacer()
            Button {
                router.path.append(.settings)
            } label: {
                Image(systemName: "gearshape")
                    .font(.system(size: 18, weight: .semibold))
                    .foregroundStyle(Theme.ink)
                    .frame(width: 44, height: 44)
                    .glass(in: Circle())
            }
            .accessibilityLabel("Settings")
            .accessibilityIdentifier("open_settings")
        }
        .padding(.top, 8)
    }

    private var streakCard: some View {
        ZStack(alignment: .topLeading) {
            SkyScene(sunrise: 0.1 + progress * 0.6, ground: 0.67, groundColor: Theme.surface, sunX: 0.6,
                     showsStars: false)
            VStack(alignment: .leading, spacing: 0) {
                HStack(alignment: .top) {
                    VStack(alignment: .leading, spacing: 4) {
                        Text("\(streak)-day streak")
                            .font(.title2.weight(.bold))
                            .contentTransition(.numericText())
                            .accessibilityIdentifier("streak_count")
                        Text(doneCount == habits.count ? "All done today. See you tomorrow."
                                                       : "\(habits.count - doneCount) left to keep it going.")
                            .font(.subheadline)
                            .foregroundStyle(Theme.secondary)
                    }
                    Spacer(minLength: 8)
                    ProgressRing(done: doneCount, total: habits.count)
                }
                Spacer(minLength: 0)
                WeekStrip(progress: progress)
            }
            .padding(.horizontal, 18)
            .padding(.top, 18)
            .padding(.bottom, 14)
        }
        .frame(height: 252)
        .clipShape(RoundedRectangle(cornerRadius: 28, style: .continuous))
        .background(
            RoundedRectangle(cornerRadius: 28, style: .continuous)
                .fill(Theme.surface)
                .shadow(color: Theme.shadow, radius: 16, y: 6)
        )
        .overlay(RoundedRectangle(cornerRadius: 28, style: .continuous).strokeBorder(Theme.hairline, lineWidth: 0.5))
        .animation(.spring(response: 0.9, dampingFraction: 1), value: doneCount)
    }

    private func binding(for id: String) -> Binding<Bool> {
        switch id {
        case "habit_water": $water
        case "habit_read": $read
        default: $walk
        }
    }
}

/// One habit: its icon, its name and a check. To VoiceOver and to checks it is a switch, value 1 or 0.
private struct HabitRow: View {
    let habit: Habit
    @Binding var isOn: Bool

    var body: some View {
        Button {
            withAnimation(.spring(response: 0.35, dampingFraction: 0.8)) { isOn.toggle() }
        } label: {
            HStack(spacing: 14) {
                HabitIcon(habit: habit, size: 44)
                VStack(alignment: .leading, spacing: 2) {
                    Text(habit.title)
                        .font(.headline)
                        .foregroundStyle(isOn ? Theme.secondary : Theme.ink)
                    Text(habit.detail)
                        .font(.subheadline)
                        .foregroundStyle(Theme.secondary)
                }
                Spacer(minLength: 8)
                CheckCircle(isOn: isOn, size: 28)
            }
            .padding(.horizontal, 16)
            .frame(height: 74)
            .contentShape(Rectangle())
        }
        .buttonStyle(PressableStyle())
        .accessibilityRepresentation {
            Toggle(habit.title, isOn: $isOn)
        }
        .accessibilityIdentifier(habit.id)
    }
}

/// A habit's symbol on a soft tile of its own colour.
struct HabitIcon: View {
    let habit: Habit
    var size: CGFloat = 44

    var body: some View {
        Image(systemName: habit.symbol)
            .font(.system(size: size * 0.4, weight: .semibold))
            .foregroundStyle(habit.tint)
            .frame(width: size, height: size)
            .background(RoundedRectangle(cornerRadius: size * 0.3, style: .continuous).fill(habit.tint.opacity(0.14)))
            .accessibilityHidden(true)
    }
}

/// An empty ring, or an ember disc with a check.
struct CheckCircle: View {
    let isOn: Bool
    var size: CGFloat = 28

    var body: some View {
        ZStack {
            Circle()
                .strokeBorder(Theme.tertiary, lineWidth: 1.5)
                .opacity(isOn ? 0 : 1)
            if isOn {
                Circle()
                    .fill(Theme.accent)
                    .transition(.scale(scale: 0.4).combined(with: .opacity))
                Image(systemName: "checkmark")
                    .font(.system(size: size * 0.42, weight: .bold))
                    .foregroundStyle(Theme.onAccent)
                    .accessibilityHidden(true)
                    .transition(.scale(scale: 0.5).combined(with: .opacity))
            }
        }
        .frame(width: size, height: size)
        .accessibilityHidden(true)
    }
}

private struct ProgressRing: View {
    let done: Int
    let total: Int

    var body: some View {
        ZStack {
            Circle()
                .stroke(Theme.ink.opacity(0.12), lineWidth: 5)
            Circle()
                .trim(from: 0, to: CGFloat(done) / CGFloat(total))
                .stroke(Theme.accent, style: StrokeStyle(lineWidth: 5, lineCap: .round))
                .rotationEffect(.degrees(-90))
            Text("\(done)/\(total)")
                .font(.footnote.weight(.semibold))
                .monospacedDigit()
                .contentTransition(.numericText())
        }
        .frame(width: 50, height: 50)
        .accessibilityElement(children: .ignore)
        .accessibilityLabel("\(done) of \(total) done")
    }
}

/// The last seven days, ending today. Past days are done (the fictional streak), today fills with progress.
private struct WeekStrip: View {
    let progress: Double

    private var days: [(label: String, date: Date, isToday: Bool, isPast: Bool)] {
        let calendar = Calendar.current
        let today = calendar.startOfDay(for: .now)
        return (-6...0).compactMap { offset in
            guard let date = calendar.date(byAdding: .day, value: offset, to: today) else { return nil }
            return (date.formatted(.dateTime.weekday(.narrow)), date, offset == 0, offset < 0)
        }
    }

    var body: some View {
        HStack(spacing: 0) {
            ForEach(days, id: \.date) { day in
                VStack(spacing: 6) {
                    Text(day.label)
                        .font(.caption2.weight(.semibold))
                        .foregroundStyle(day.isToday ? Theme.accent : Theme.tertiary)
                    ZStack {
                        if day.isPast {
                            Circle().fill(Theme.accent)
                            Image(systemName: "checkmark")
                                .font(.system(size: 11, weight: .bold))
                                .foregroundStyle(Theme.onAccent)
                        } else {
                            Circle().strokeBorder(Theme.hairline, lineWidth: 3)
                            Circle()
                                .trim(from: 0, to: progress)
                                .stroke(Theme.accent, style: StrokeStyle(lineWidth: 3, lineCap: .round))
                                .rotationEffect(.degrees(-90))
                                .padding(1.5)
                            Text(day.date.formatted(.dateTime.day()))
                                .font(.caption.weight(.semibold))
                                .foregroundStyle(Theme.ink)
                        }
                    }
                    .frame(width: 32, height: 32)
                }
                .frame(maxWidth: .infinity)
            }
        }
        .accessibilityElement(children: .ignore)
        .accessibilityLabel("Last 7 days")
        .accessibilityValue("\(days.filter(\.isPast).count) of 7 done")
    }
}
