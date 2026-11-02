//
//  Widget.swift — "Thesis Planner Bar"
//
//  A menu-bar-only (LSUIElement) day-at-a-glance widget for the thesis planner.
//  It reads planner/data/planner.db directly through the system SQLite3
//  library: no web server, no Python, no third-party dependencies.
//
//  Build:  packaging/macos/build_menubar_app.sh [--install]
//  Test:   "Thesis Planner Bar.app/Contents/MacOS/ThesisPlannerBar" \
//              --dump [--db /path/to/planner.db]
//
//  Database resolution order: --db PATH, $PLANNER_DB, compiled-in default.
//  The current plan is the newest `plans` row whose `week` is this week's
//  Monday; only that plan's slots are shown, so a re-plan replaces the view.
//

import AppKit
import Dispatch
import Foundation
import SQLite3

// MARK: - Constants

private let kDefaultDBPath = "/Users/macos/Desktop/commonsense-atlas/planner/data/planner.db"
private let kRepoRoot = "/Users/macos/Desktop/commonsense-atlas"
private let kTitleBudget = 26
private let kMaxMenuRows = 6
private let kWebURL = "http://127.0.0.1:8765/"

// MARK: - Formatting

private let dayFormatter: DateFormatter = {
    let f = DateFormatter()
    f.locale = Locale(identifier: "en_US_POSIX")
    f.timeZone = TimeZone.current
    f.dateFormat = "yyyy-MM-dd"
    return f
}()

private let dayLabelFormatter: DateFormatter = {
    let f = DateFormatter()
    f.locale = Locale(identifier: "en_US_POSIX")
    f.timeZone = TimeZone.current
    f.dateFormat = "EEE d MMM"
    return f
}()

private let weekdayFormatter: DateFormatter = {
    let f = DateFormatter()
    f.locale = Locale(identifier: "en_US_POSIX")
    f.timeZone = TimeZone.current
    f.dateFormat = "EEE"
    return f
}()

private func gregorianCalendar() -> Calendar {
    var cal = Calendar(identifier: .gregorian)
    cal.locale = Locale(identifier: "en_US_POSIX")
    cal.timeZone = TimeZone.current
    return cal
}

/// Minutes since midnight for "HH:MM"; nil when the value is malformed.
private func minutesFromHHMM(_ value: String) -> Int? {
    let parts = value.split(separator: ":")
    guard parts.count >= 2,
          let hour = Int(parts[0]),
          let minute = Int(parts[1]) else { return nil }
    return hour * 60 + minute
}

private func currentMinutes(_ date: Date) -> Int {
    let comps = gregorianCalendar().dateComponents([.hour, .minute], from: date)
    return (comps.hour ?? 0) * 60 + (comps.minute ?? 0)
}

/// Monday of the week containing `date`, as YYYY-MM-DD.
private func mondayString(for date: Date) -> String {
    let cal = gregorianCalendar()
    let start = cal.startOfDay(for: date)
    let weekday = cal.component(.weekday, from: start) // 1 = Sunday
    let daysBack = (weekday + 5) % 7                  // Monday = 0
    let monday = cal.date(byAdding: .day, value: -daysBack, to: start) ?? start
    return dayFormatter.string(from: monday)
}

/// Trim to `limit` characters, marking the cut with an ellipsis.
private func truncate(_ value: String, _ limit: Int) -> String {
    if limit <= 0 { return "" }
    if value.count <= limit { return value }
    if limit == 1 { return "…" }
    return String(value.prefix(limit - 1)) + "…"
}

// MARK: - Model

private struct Slot {
    let date: String
    let start: String
    let end: String
    let minutes: Int
    let title: String
    let status: String
    let subtask: String

    var isDone: Bool { status == "done" }
}

private struct Snapshot {
    var today: String = ""
    var todayLabel: String = ""
    var todayMinutes: Int = 0
    var todayBlocks: Int = 0
    var weekMinutes: Int = 0
    var weekBlocks: Int = 0
    var todaySlots: [Slot] = []
    var nextToday: Slot?
    var nextLater: Slot?
    var nextLaterDayLabel: String?
    var dbPath: String = ""
    var dbOpened: Bool = false
}

// MARK: - SQLite (read-only)

private func openReadOnly(_ path: String) -> OpaquePointer? {
    guard FileManager.default.fileExists(atPath: path) else { return nil }
    var handle: OpaquePointer?
    let flags = SQLITE_OPEN_READONLY | SQLITE_OPEN_FULLMUTEX
    let rc = sqlite3_open_v2(path, &handle, flags, nil)
    if rc != SQLITE_OK {
        if let opened = handle { sqlite3_close(opened) }
        return nil
    }
    guard let db = handle else { return nil }
    sqlite3_busy_timeout(db, 750)
    return db
}

private func query(_ db: OpaquePointer, _ sql: String, _ row: (OpaquePointer) -> Void) {
    var stmt: OpaquePointer?
    let rc = sqlite3_prepare_v2(db, sql, -1, &stmt, nil)
    guard rc == SQLITE_OK, let prepared = stmt else {
        if let partial = stmt { sqlite3_finalize(partial) }
        return
    }
    defer { sqlite3_finalize(prepared) }
    while sqlite3_step(prepared) == SQLITE_ROW {
        row(prepared)
    }
}

private func text(_ stmt: OpaquePointer, _ index: Int32) -> String {
    guard let raw = sqlite3_column_text(stmt, index) else { return "" }
    return String(cString: raw)
}

private func hasColumn(_ db: OpaquePointer, table: String, column: String) -> Bool {
    var found = false
    query(db, "PRAGMA table_info(\(table))") { stmt in
        if text(stmt, 1) == column { found = true }
    }
    return found
}

private func firstInt(_ db: OpaquePointer, _ sql: String) -> Int? {
    var value: Int?
    query(db, sql) { stmt in
        if value == nil { value = Int(sqlite3_column_int64(stmt, 0)) }
    }
    return value
}

// MARK: - Snapshot loading

private func loadSnapshot(dbPath: String, now: Date = Date()) -> Snapshot {
    var snap = Snapshot()
    snap.dbPath = dbPath
    snap.today = dayFormatter.string(from: now)
    snap.todayLabel = dayLabelFormatter.string(from: now)

    let monday = mondayString(for: now)
    let nowMinutes = currentMinutes(now)

    guard let db = openReadOnly(dbPath) else { return snap }
    defer { sqlite3_close(db) }
    snap.dbOpened = true

    // The shipped schema has slots.calendar_event_id; the brief also mentions a
    // `subtask` column. Read it when present, ignore it when absent.
    let subtaskExpr = hasColumn(db, table: "slots", column: "subtask") ? "s.subtask" : "''"

    guard let planID = firstInt(db, "SELECT id FROM plans WHERE week = '\(monday)' ORDER BY id DESC LIMIT 1") else {
        return snap
    }

    let sql = """
    SELECT s.date, s.start, s.end, s.minutes, \(subtaskExpr),
           COALESCE(t.title, '(task removed)'),
           COALESCE(t.status, 'todo')
    FROM slots s
    LEFT JOIN tasks t ON t.id = s.task_id
    WHERE s.plan_id = \(planID)
    ORDER BY s.date, s.start
    """

    var allSlots: [Slot] = []
    query(db, sql) { stmt in
        let slot = Slot(date: text(stmt, 0),
                        start: text(stmt, 1),
                        end: text(stmt, 2),
                        minutes: Int(sqlite3_column_int(stmt, 3)),
                        title: text(stmt, 5),
                        status: text(stmt, 6),
                        subtask: text(stmt, 4))
        allSlots.append(slot)
    }

    snap.weekBlocks = allSlots.count
    snap.weekMinutes = allSlots.reduce(0) { $0 + $1.minutes }
    snap.todaySlots = allSlots.filter { $0.date == snap.today }
    snap.todayBlocks = snap.todaySlots.count
    snap.todayMinutes = snap.todaySlots.reduce(0) { $0 + $1.minutes }

    snap.nextToday = snap.todaySlots.first {
        (minutesFromHHMM($0.end) ?? 0) > nowMinutes
    }

    // Fallback for the status title when the day is empty but the week is not:
    // the next block on a later day, labelled with its weekday.
    for slot in allSlots where slot.date > snap.today {
        if let day = dayFormatter.date(from: slot.date) {
            snap.nextLater = slot
            snap.nextLaterDayLabel = weekdayFormatter.string(from: day)
            break
        }
    }

    return snap
}

// MARK: - Rendering

private func titleText(_ snap: Snapshot) -> String {
    if let next = snap.nextToday {
        let prefix = "▸\(next.start) "
        return prefix + truncate(next.title, kTitleBudget - prefix.count)
    }

    if snap.todayBlocks > 0 {
        let noun = snap.todayBlocks == 1 ? "block" : "blocks"
        return "· \(snap.todayBlocks) \(noun) \(snap.todayMinutes)m"
    }

    if let next = snap.nextLater {
        let day = snap.nextLaterDayLabel ?? ""
        let prefix = day.isEmpty ? "▸\(next.start) " : "▸\(day) \(next.start) "
        return prefix + truncate(next.title, kTitleBudget - prefix.count)
    }

    return "○ nothing planned"
}

private func headerText(_ snap: Snapshot) -> String {
    var out = "\(snap.todayLabel) · \(snap.todayMinutes)m planned"
    if snap.weekMinutes > snap.todayMinutes {
        out += " · \(snap.weekMinutes)m this week"
    }
    return out
}

private func rowText(_ slot: Slot) -> String {
    let mark = slot.isDone ? "✓ " : ""
    var out = "\(slot.start)–\(slot.end)  \(mark)\(slot.title)"
    if !slot.subtask.isEmpty {
        out += " · \(truncate(slot.subtask, 40))"
    }
    return out
}

private func nextRowText(_ slot: Slot, dayLabel: String?) -> String {
    let day = dayLabel.map { "\($0) " } ?? ""
    return "Next: \(day)\(slot.start)–\(slot.end)  \(truncate(slot.title, 44))"
}

private func tooltipText(_ snap: Snapshot) -> String {
    let noun = snap.todayBlocks == 1 ? "block" : "blocks"
    return "Thesis Planner — \(snap.todayLabel): \(snap.todayBlocks) \(noun), \(snap.todayMinutes)m"
}

// MARK: - Command line

private struct Options {
    var dbPath: String
    var dump: Bool
}

private func printUsage() {
    print("""
    Thesis Planner Bar — menu-bar widget for the thesis planner

    Usage:
      ThesisPlannerBar [--dump] [--db PATH]

      --dump      print the status title and today's menu rows, then exit
      --db PATH   planner SQLite database
                  (default: $PLANNER_DB or \(kDefaultDBPath))
    """)
}

private func parseArguments() -> Options {
    var dbPath = ProcessInfo.processInfo.environment["PLANNER_DB"]?
        .trimmingCharacters(in: .whitespacesAndNewlines) ?? ""
    if dbPath.isEmpty { dbPath = kDefaultDBPath }

    var dump = false
    var args = CommandLine.arguments.dropFirst()
    while let arg = args.popFirst() {
        if arg == "--dump" {
            dump = true
        } else if arg == "--db" {
            if let value = args.popFirst(), !value.isEmpty { dbPath = value }
        } else if arg.hasPrefix("--db=") {
            let value = String(arg.dropFirst("--db=".count))
            if !value.isEmpty { dbPath = value }
        } else if arg == "--help" || arg == "-h" {
            printUsage()
            exit(0)
        }
    }

    return Options(dbPath: (dbPath as NSString).expandingTildeInPath, dump: dump)
}

private func runDump(dbPath: String) -> Never {
    let snap = loadSnapshot(dbPath: dbPath)

    print("title: \(titleText(snap))")
    print("header: \(headerText(snap))")
    if snap.todaySlots.isEmpty {
        print("row: (no blocks today)")
    } else {
        for slot in snap.todaySlots.prefix(kMaxMenuRows) {
            print("row: \(rowText(slot))")
        }
        let hidden = snap.todaySlots.count - kMaxMenuRows
        if hidden > 0 { print("row: (+\(hidden) more today)") }
    }
    if snap.todaySlots.isEmpty, let next = snap.nextLater {
        print("row: \(nextRowText(next, dayLabel: snap.nextLaterDayLabel))")
    }
    print("db: \(snap.dbOpened ? "ok" : "missing") \(snap.dbPath)")
    exit(0)
}

// MARK: - Menu-bar app

final class AppDelegate: NSObject, NSApplicationDelegate, NSMenuDelegate {
    private let dbPath: String
    private var statusItem: NSStatusItem?
    private var timer: Timer?
    private var replanProcess: Process?
    private var isReplanning = false
    private var menuIsOpen = false

    init(dbPath: String) {
        self.dbPath = dbPath
        super.init()
    }

    func applicationDidFinishLaunching(_ notification: Notification) {
        _ = NSApp.setActivationPolicy(.accessory)

        let item = NSStatusBar.system.statusItem(withLength: NSStatusItem.variableLength)
        statusItem = item
        if let button = item.button {
            button.font = NSFont.monospacedDigitSystemFont(ofSize: 12, weight: .regular)
            button.toolTip = "Thesis Planner"
        }

        let menu = NSMenu()
        menu.autoenablesItems = false
        menu.delegate = self
        item.menu = menu

        refresh()

        let tick = Timer(timeInterval: 60, repeats: true) { [weak self] _ in
            self?.refresh()
        }
        RunLoop.main.add(tick, forMode: .common)
        timer = tick
    }

    func applicationWillTerminate(_ notification: Notification) {
        timer?.invalidate()
        timer = nil
    }

    func menuNeedsUpdate(_ menu: NSMenu) {
        refresh()
    }

    func menuWillOpen(_ menu: NSMenu) {
        menuIsOpen = true
    }

    func menuDidClose(_ menu: NSMenu) {
        menuIsOpen = false
    }

    private func refresh() {
        guard let item = statusItem else { return }
        let snap = loadSnapshot(dbPath: dbPath)
        item.button?.title = isReplanning ? "⋯ re-planning" : titleText(snap)
        item.button?.toolTip = tooltipText(snap)
        // Rebuilding an open menu would dismiss it; the 60 s tick only needs
        // to refresh the title while the menu is on screen.
        if !menuIsOpen {
            rebuildMenu(snap)
        }
    }

    private func rebuildMenu(_ snap: Snapshot) {
        guard let menu = statusItem?.menu else { return }
        menu.removeAllItems()

        let header = NSMenuItem(title: headerText(snap), action: nil, keyEquivalent: "")
        header.isEnabled = false
        header.attributedTitle = NSAttributedString(string: headerText(snap), attributes: [
            .font: NSFont.systemFont(ofSize: 11, weight: .semibold),
            .foregroundColor: NSColor.labelColor
        ])
        menu.addItem(header)

        if snap.todaySlots.isEmpty {
            let empty = NSMenuItem(title: "No blocks today", action: nil, keyEquivalent: "")
            empty.isEnabled = false
            menu.addItem(empty)
        } else {
            for slot in snap.todaySlots.prefix(kMaxMenuRows) {
                menu.addItem(rowItem(slot))
            }
            let hidden = snap.todaySlots.count - kMaxMenuRows
            if hidden > 0 {
                let more = NSMenuItem(title: "+\(hidden) more today", action: nil, keyEquivalent: "")
                more.isEnabled = false
                menu.addItem(more)
            }
        }

        if snap.todaySlots.isEmpty, let next = snap.nextLater {
            let nextItem = NSMenuItem(title: nextRowText(next, dayLabel: snap.nextLaterDayLabel),
                                      action: nil, keyEquivalent: "")
            nextItem.isEnabled = false
            menu.addItem(nextItem)
        }

        menu.addItem(NSMenuItem.separator())
        menu.addItem(actionItem("Open Thesis Planner", #selector(openPlanner), "o"))
        let replan = actionItem("Re-plan now", #selector(replanNow), "r")
        replan.isEnabled = !isReplanning
        menu.addItem(replan)
        menu.addItem(actionItem("Refresh", #selector(refreshFromMenu), ""))
        menu.addItem(NSMenuItem.separator())
        menu.addItem(actionItem("Quit", #selector(quit), "q"))
    }

    private func rowItem(_ slot: Slot) -> NSMenuItem {
        let title = rowText(slot)
        let item = NSMenuItem(title: title, action: nil, keyEquivalent: "")
        item.isEnabled = false
        var attributes: [NSAttributedString.Key: Any] = [
            .font: NSFont.monospacedDigitSystemFont(ofSize: 12, weight: .regular),
            .foregroundColor: NSColor.labelColor
        ]
        if slot.isDone {
            attributes[.foregroundColor] = NSColor.secondaryLabelColor
            attributes[.strikethroughStyle] = NSUnderlineStyle.single.rawValue
        }
        item.attributedTitle = NSAttributedString(string: title, attributes: attributes)
        return item
    }

    private func actionItem(_ title: String, _ selector: Selector, _ key: String) -> NSMenuItem {
        let item = NSMenuItem(title: title, action: selector, keyEquivalent: key)
        item.target = self
        item.isEnabled = true
        return item
    }

    @objc private func refreshFromMenu() {
        refresh()
    }

    @objc private func openPlanner() {
        let appPath = (NSHomeDirectory() as NSString)
            .appendingPathComponent("Applications/Thesis Planner.app")
        if FileManager.default.fileExists(atPath: appPath) {
            _ = NSWorkspace.shared.open(URL(fileURLWithPath: appPath))
            return
        }
        if let url = URL(string: kWebURL) {
            _ = NSWorkspace.shared.open(url)
        }
    }

    @objc private func replanNow() {
        guard !isReplanning else { return }

        let process = Process()
        process.executableURL = URL(fileURLWithPath: "/usr/bin/python3")
        process.arguments = ["-m", "planner.cli", "auto"]
        process.currentDirectoryURL = URL(fileURLWithPath: repoRoot(forDB: dbPath))

        var environment = ProcessInfo.processInfo.environment
        environment["PLANNER_DB"] = dbPath
        process.environment = environment

        process.standardOutput = FileHandle.nullDevice
        process.standardError = FileHandle.nullDevice
        process.terminationHandler = { [weak self] _ in
            DispatchQueue.main.async {
                guard let self = self else { return }
                self.isReplanning = false
                self.replanProcess = nil
                self.refresh()
            }
        }

        do {
            isReplanning = true
            try process.run()
            replanProcess = process
            statusItem?.button?.title = "⋯ re-planning"
        } catch {
            isReplanning = false
            replanProcess = nil
            refresh()
        }
    }

    @objc private func quit() {
        NSApp.terminate(nil)
    }
}

/// Locate the repo root that owns the database, so "Re-plan now" can run
/// `python3 -m planner.cli` from a directory where the package is importable.
private func repoRoot(forDB path: String) -> String {
    let marker = "/planner/data/"
    if let range = path.range(of: marker, options: .backwards) {
        let root = String(path[..<range.lowerBound])
        if !root.isEmpty { return root }
    }
    return kRepoRoot
}

// MARK: - Entry point

@main
struct WidgetMain {
    static func main() {
        let options = parseArguments()

        if options.dump {
            runDump(dbPath: options.dbPath)
        }

        let app = NSApplication.shared
        let delegate = AppDelegate(dbPath: options.dbPath)
        app.delegate = delegate
        _ = app.setActivationPolicy(.accessory)

        withExtendedLifetime(delegate) {
            app.run()
        }
    }
}
