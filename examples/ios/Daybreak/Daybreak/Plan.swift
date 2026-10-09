import Foundation

/// A Daybreak Plus subscription plan. Prices are fixed: this sample sells nothing.
struct Plan: Identifiable, Hashable {
    enum Period: String {
        case week, month, year
    }

    /// Also the plan button's accessibility identifier.
    let id: String
    let name: String
    let price: String
    let period: Period
    let detail: String
    let badge: String?

    /// "$39.99 / year", the plan button's accessibility value.
    var priceLine: String { "\(price) / \(period.rawValue)" }

    static let weekly = Plan(id: "plan_weekly", name: "Weekly", price: "$2.99", period: .week,
                             detail: "Billed every week", badge: nil)
    static let monthly = Plan(id: "plan_monthly", name: "Monthly", price: "$7.99", period: .month,
                              detail: "Billed every month", badge: nil)
    static var annual: Plan {
        Plan(id: "plan_annual", name: "Annual", price: "$39.99",
             period: Bug.annualPrice.isOn ? .month : .year,
             detail: "$3.33 a month, billed yearly", badge: "Best value")
    }

    /// The plans the paywall offers, in order.
    static var all: [Plan] {
        let plans = [weekly, monthly, annual]
        return Bug.missingPlan.isOn ? Array(plans.prefix(2)) : plans
    }

    static func named(_ id: Plan.ID) -> Plan {
        [weekly, monthly, annual].first { $0.id == id } ?? annual
    }
}
