import SwiftUI

@main
struct DaybreakApp: App {
    @State private var router = AppRouter()

    var body: some Scene {
        WindowGroup {
            RootView()
                .environment(router)
        }
    }
}

/// Onboarding on the first launch, then Today. The paywall covers either one.
struct RootView: View {
    @Environment(AppRouter.self) private var router

    var body: some View {
        @Bindable var router = router
        ZStack {
            if router.hasOnboarded {
                NavigationStack(path: $router.path) {
                    TodayView()
                        .navigationDestination(for: AppRouter.Route.self) { route in
                            switch route {
                            case .settings: SettingsView()
                            }
                        }
                }
                .transition(.opacity)
            } else {
                OnboardingView()
                    .transition(.opacity)
            }
        }
        .fullScreenCover(isPresented: $router.showPaywall) {
            PaywallView()
        }
        .onOpenURL { router.open($0) }
        .tint(Theme.accent)
    }
}
