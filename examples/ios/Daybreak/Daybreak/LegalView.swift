import SwiftUI

enum LegalPage: String, Identifiable {
    case terms, privacy

    var id: String { rawValue }

    var title: String {
        switch self {
        case .terms: "Terms"
        case .privacy: "Privacy"
        }
    }

    var paragraphs: [String] {
        switch self {
        case .terms:
            [
                "Daybreak is a sample app. It exists to show how Mobster checks the screens of an iOS app.",
                "Daybreak Plus is not for sale. The plans, prices and free trial on the paywall are examples, and the Start free trial button charges nothing.",
                "The app and its source are provided as is, under the MIT License.",
            ]
        case .privacy:
            [
                "Daybreak keeps your habits and settings on this iPhone. It has no account and no analytics, and it makes no network requests.",
                "Reminders are local notifications that your iPhone schedules for itself.",
                "Deleting the app deletes everything it stored.",
            ]
        }
    }
}

struct LegalView: View {
    let page: LegalPage
    @Environment(\.dismiss) private var dismiss

    var body: some View {
        NavigationStack {
            ScrollView {
                VStack(alignment: .leading, spacing: 16) {
                    ForEach(page.paragraphs, id: \.self) { paragraph in
                        Text(paragraph)
                            .font(.body)
                            .foregroundStyle(Theme.secondary)
                            .fixedSize(horizontal: false, vertical: true)
                    }
                }
                .frame(maxWidth: .infinity, alignment: .leading)
                .padding(20)
            }
            .background(Theme.background.ignoresSafeArea())
            .navigationTitle(page.title)
            .navigationBarTitleDisplayMode(.inline)
            .toolbar {
                ToolbarItem(placement: .confirmationAction) {
                    Button("Done") { dismiss() }
                }
            }
        }
        .presentationDetents([.medium, .large])
    }
}
