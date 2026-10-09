import SwiftUI

/// Daybreak Plus: three plans, Annual picked by default. Shown after onboarding, from Settings,
/// and by daybreak://paywall.
struct PaywallView: View {
    @Environment(\.dismiss) private var dismiss
    @State private var selected: Plan.ID = Plan.annual.id
    @State private var loaded = false
    @State private var showPurchaseAlert = false
    @State private var showRestoreAlert = false
    @State private var legal: LegalPage?

    var body: some View {
        ZStack(alignment: .topTrailing) {
            Theme.background.ignoresSafeArea()
            ScrollView {
                VStack(spacing: 0) {
                    SkyScene(sunrise: 0.6, ground: 0.9, sunX: 0.5, risesOnAppear: true)
                        .frame(height: 296)
                    header
                        .padding(.top, -22)
                    plans
                        .padding(.top, 22)
                }
                .padding(.bottom, 8)
            }
            .ignoresSafeArea(.container, edges: .top)
            .scrollBounceBehavior(.basedOnSize)
            .scrollIndicators(.hidden)
            .safeAreaInset(edge: .bottom, spacing: 0) { footer }
            closeButton
        }
        .task { await loadPlans() }
        .sensoryFeedback(.selection, trigger: selected)
        .alert("Purchases are off in this sample", isPresented: $showPurchaseAlert) {
            Button("OK", role: .cancel) {}
        } message: {
            Text("Daybreak shows how a paywall gets checked. Nothing was charged.")
        }
        .alert("Nothing to restore", isPresented: $showRestoreAlert) {
            Button("OK", role: .cancel) {}
        } message: {
            Text("This sample has no purchases.")
        }
        .sheet(item: $legal) { page in
            LegalView(page: page)
        }
    }

    private var closeButton: some View {
        Button("Not now") { dismiss() }
            .font(.subheadline.weight(.semibold))
            .foregroundStyle(Theme.ink)
            .padding(.horizontal, 14)
            .frame(height: 34)
            .glass(in: Capsule())
            .padding(.trailing, 16)
            .padding(.top, 6)
            .accessibilityIdentifier("paywall_close")
    }

    private var header: some View {
        VStack(spacing: 6) {
            Text("DAYBREAK PLUS")
                .font(.footnote.weight(.semibold))
                .tracking(1.6)
                .foregroundStyle(Theme.accent)
            Text("Choose your plan")
                .font(.largeTitle.weight(.bold))
                .foregroundStyle(Theme.ink)
                .accessibilityIdentifier("paywall_title")
                .accessibilityAddTraits(.isHeader)
            VStack(alignment: .leading, spacing: 9) {
                Benefit(symbol: "infinity", text: "Unlimited habits")
                Benefit(symbol: "chart.bar.fill", text: "Streak insights and history")
                Benefit(symbol: "bell.fill", text: "Reminders at the time you choose")
            }
            .padding(.top, 10)
        }
        .padding(.horizontal, 36)
    }

    private var plans: some View {
        Group {
            if loaded {
                VStack(spacing: 10) {
                    ForEach(Plan.all) { plan in
                        PlanCard(plan: plan, isSelected: plan.id == selected) {
                            withAnimation(.spring(response: 0.3, dampingFraction: 1)) { selected = plan.id }
                        }
                    }
                }
                .transition(.opacity)
            } else {
                VStack(spacing: 12) {
                    ProgressView()
                        .controlSize(.regular)
                        .tint(Theme.secondary)
                    Text("Loading plans…")
                        .font(.subheadline)
                        .foregroundStyle(Theme.secondary)
                }
                .frame(maxWidth: .infinity, minHeight: 230)
            }
        }
        .padding(.horizontal, 20)
    }

    private var footer: some View {
        VStack(spacing: 12) {
            Button("Start free trial") { showPurchaseAlert = true }
                .buttonStyle(PrimaryButtonStyle())
                .disabled(!loaded)
                .accessibilityIdentifier("paywall_cta")
            Text("7 days free, then \(Plan.named(selected).priceLine). Cancel anytime.")
                .font(.footnote)
                .foregroundStyle(Theme.secondary)
                .contentTransition(.numericText())
            HStack(spacing: 8) {
                Button("Restore Purchases") { showRestoreAlert = true }
                    .accessibilityIdentifier("paywall_restore")
                Text("·").accessibilityHidden(true)
                Button("Terms") { legal = .terms }
                    .accessibilityRemoveTraits(.isButton)
                    .accessibilityAddTraits(.isLink)
                Text("·").accessibilityHidden(true)
                Button("Privacy") { legal = .privacy }
                    .accessibilityRemoveTraits(.isButton)
                    .accessibilityAddTraits(.isLink)
            }
            .font(.footnote.weight(.medium))
            .foregroundStyle(Theme.secondary)
        }
        .padding(.horizontal, 20)
        .padding(.top, 14)
        .padding(.bottom, 4)
        .background(
            LinearGradient(stops: [.init(color: Theme.background.opacity(0), location: 0),
                                   .init(color: Theme.background, location: 0.18)],
                           startPoint: .top, endPoint: .bottom)
                .ignoresSafeArea()
        )
    }

    /// Stands in for asking the App Store for prices.
    private func loadPlans() async {
        guard !Bug.stuckLoading.isOn else { return }
        try? await Task.sleep(for: .milliseconds(350))
        withAnimation(.easeOut(duration: 0.25)) { loaded = true }
    }
}

/// One plan as a selectable card. Its accessibility label is the plan's name and its value the price line.
struct PlanCard: View {
    let plan: Plan
    let isSelected: Bool
    let action: () -> Void

    var body: some View {
        Button(action: action) {
            HStack(spacing: 14) {
                SelectionMark(isSelected: isSelected)
                VStack(alignment: .leading, spacing: 3) {
                    HStack(spacing: 8) {
                        Text(plan.name)
                            .font(.headline)
                        if let badge = plan.badge {
                            Text(badge)
                                .font(.caption2.weight(.bold))
                                .textCase(.uppercase)
                                .tracking(0.5)
                                .foregroundStyle(Theme.onAccent)
                                .padding(.horizontal, 7)
                                .padding(.vertical, 3)
                                .background(Capsule().fill(Theme.accent))
                        }
                    }
                    Text(plan.detail)
                        .font(.subheadline)
                        .foregroundStyle(Theme.secondary)
                }
                Spacer(minLength: 8)
                VStack(alignment: .trailing, spacing: 2) {
                    Text(plan.price)
                        .font(.headline)
                        .monospacedDigit()
                    Text("/ \(plan.period.rawValue)")
                        .font(.footnote)
                        .foregroundStyle(Theme.secondary)
                }
            }
            .foregroundStyle(Theme.ink)
            .padding(.horizontal, 18)
            .frame(minHeight: 66)
            .background(
                RoundedRectangle(cornerRadius: 20, style: .continuous)
                    .fill(isSelected ? Theme.selectedSurface : Theme.surface)
                    .shadow(color: isSelected ? Theme.accentGlow : Theme.shadow, radius: isSelected ? 14 : 8,
                            y: isSelected ? 5 : 2)
            )
            .overlay(
                RoundedRectangle(cornerRadius: 20, style: .continuous)
                    .strokeBorder(isSelected ? Theme.accent : Theme.cardEdge, lineWidth: isSelected ? 2 : 1)
            )
            .contentShape(RoundedRectangle(cornerRadius: 20, style: .continuous))
        }
        .buttonStyle(PressableStyle())
        .accessibilityLabel(plan.name)
        .accessibilityValue(plan.priceLine)
        .accessibilityIdentifier(plan.id)
        .accessibilityAddTraits(isSelected ? .isSelected : [])
    }
}

/// One line of what Plus adds, with its symbol on a soft ember disc.
private struct Benefit: View {
    let symbol: String
    let text: String

    var body: some View {
        HStack(spacing: 12) {
            Image(systemName: symbol)
                .font(.system(size: 11, weight: .bold))
                .foregroundStyle(Theme.accent)
                .frame(width: 24, height: 24)
                .background(Circle().fill(Theme.accentSoft))
                .accessibilityHidden(true)
            Text(text)
                .font(.subheadline.weight(.medium))
                .foregroundStyle(Theme.ink)
        }
    }
}

/// A radio mark that fills with ember and draws a check when its plan is picked.
private struct SelectionMark: View {
    let isSelected: Bool

    var body: some View {
        ZStack {
            Circle()
                .strokeBorder(isSelected ? Theme.accent : Theme.tertiary, lineWidth: 1.5)
            if isSelected {
                // Only the picked plan carries a check, so no other card reads as selected.
                Circle()
                    .fill(Theme.accent)
                    .transition(.scale(scale: 0.2).combined(with: .opacity))
                Image(systemName: "checkmark")
                    .font(.system(size: 11, weight: .bold))
                    .foregroundStyle(Theme.onAccent)
                    .accessibilityHidden(true)
                    .transition(.scale(scale: 0.5).combined(with: .opacity))
            }
        }
        .frame(width: 24, height: 24)
        .accessibilityHidden(true)
    }
}

#Preview("Paywall") {
    PaywallView()
}
